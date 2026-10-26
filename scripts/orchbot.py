"""Orchestrator bot, SHADOW MODE v0 (conveyor redesign stage 3).

One pass reads conveyor state - the build/conveyor/events.jsonl tail since the last cursor,
lanes.json, backlog.json, pager.json, status.json (attention), the lane reports and build/approvals -
and appends what it WOULD do to build/bot/decisions.jsonl; build/bot/shadow.json (atomic) holds the
cursor, counters, rule memory and the last 50 decisions for the pano. It never acts: no git, no
network, no model calls, never data/jarvis.db. Rules: ORCH_RULES O5 O10 O15 O19 O23 O26 O33 and
EVTAUDIT (R4, R7, #1). A bad input logs one escalate_orchestrator decision; a pass never crashes.
BOTAGREE: each pass also observes what the orchestrator/conveyor actually did (ack files, approvals,
new backlog ids, pace.json, page / ci_rerun events), matches it to the bot's decisions on the same
target within MATCH_S, logs one "verdict" line per decided match and keeps shadow.json "agreement".
BOTFACTS: each pass also writes build/bot/facts.md (atomic, <= FACTS_MAX bytes): the FACT part of an
orchestrator handoff rendered by code from the files, every line naming its source; a missing or
unreadable source prints "unknown (<file>)", never a guess.
BOTASKS: each pass also writes build/bot/asks.json (atomic): what Salih WOULD be asked under
"Sizden beklenen" (SALIH RULE O39) - key 1 for a CONVSAFE range, a conveyor restart after a pushed
scripts/conveyor.py change, a T2 approval keeping the queue dry - in the build/pano/build.py ask shape.
An ask closes when its condition clears; open/close log one decision each; unknown input gives no
ask and one escalate. Ask text is redacted and never names a path outside the repo.

BOTLIVE: build/bot/live.json {"ack", "asks", "hold_dispatch", "page_salih": bool} turns single actions live;
missing/malformed = all shadow, build/bot/OFF = all shadow (checked every pass). Live writes only
build/conveyor/ack/<name>, build/conveyor/PAUSE (text "bot: <rule>", removed only when the bot made it) and
the asks.json pano marker; each one logs a decisions.jsonl line with mode "live", before/after and an undo
hint. page_salih never goes live: the conveyor pager reads only build/conveyor/pager.json. A loop guard
trip demotes that action to shadow until live.demoted.<action> is deleted from shadow.json.

    python scripts/orchbot.py --shadow [--root DIR] [--once] [--quota-ceiling PCT]
"""
import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import secrets
import sys
import time
import urllib.request
from datetime import datetime
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
# BOTFIX3 orchestrator wake: one coalesced ntfy message per WAKE_S on these rules; never the human paging topic
WAKE_TOPIC = Path.home() / ".jarvis" / "orch_wake.topic"
WAKE_S = 120
STUCK_S = 3 * 3600  # a working lane with no status update for 3 h
WAKE = {"O5", "O10", "DRY", "CONVSAFE", "R7/O19", "STUCK", "EVTAUDIT1", "PAGER", "O23", "HOLD", "BREAKER",
        "ATTN", "LOOP", "FAILCLOSED"}
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")  # unguessable: token_urlsafe(24) gives 32
REPORT_MAX = 256 * 1024
ROUTINE_RE = re.compile(r"^(reader needs_orchestrator|FINDINGS):")
# the only cleanup refusals raised after the work was proven on main (conveyor remove_worktree: worktree
# remove after the clean+integrated checks, branch delete after the off-main check); any other is kept
PROVEN_RE = re.compile(r"^cleanup refused \(git (?:worktree remove|update-ref -d \S+) rc=-?\d+: ")
FILES_RE = re.compile(r"(?m)^[ \t]*(?:[A-Z]+ )*FILES:[ \t]*(.*)$")
PICKS_RE = re.compile(r"(?m)^PICKS\b([^\n]*)(?:\n|$)((?:(?:[ \t]*\n)*[ \t]+\S.*(?:\n|$))*)")  # an INT range
PICK_TOK_RE = re.compile(r"\b[A-Z][A-Z0-9_-]{1,63}\b")  # no '.' or '/': a lane id, never a path
TS_FMT = "%Y-%m-%dT%H:%M:%S"
HEAD_N = 256  # events.jsonl rotation fingerprint: a hash of its first bytes
MATCH_S = 3600  # BOTAGREE: a decision and an actual action on one target within 60 min are one match
# BOTFIX3B: an ack / ask / key-1 / fix waits up to a day for the orchestrator; a fix may land 5 min early
DAY_S, FIX_EARLY_S, AGREE_VERSION = 86400, 300, 4
# BOTFIX3D: words a board ask uses for a bot ask without a lane/id in it
ASK_WORDS = {"restart": ("restart", "yeniden başlat")}  # ponytail: one alias set, grow it per new board wording
SWEEP_N = 10  # BOTFIX3C: this many orchestrator acks in one minute are a sweep, not a judgement
SALIH_RE = re.compile(r"(?i)\bby salih\b|salih onay|chat .*onay|manual key 1")
PRE_RE = re.compile(r"(?i)under O42|pre-approval")  # pre-approved: not an orchestrator key-1 call
STAGE_A, STAGE_PCT = {"ack": 30, "ask": 10}, 90  # stage A: ack + ask agree >= 90% on enough samples
RECENT_N = 10
GRAD_DAYS, GRAD_PUSHES = 7, 10  # stage 4 exit: >= 1 week, >= 10 key-1 decisions, 100% agree
KEY1 = {"write_key1", "ask_salih_key1"}
STAMP_RE = re.compile(r"\.\d{8}-\d{6}$")  # approvals/done/<name>.<YYYYmmdd-HHMMSS> on a name clash
PACE_KEYS = ("max_lanes", "min_lanes", "lanes_present", "lanes_away")  # a decrease = dispatch held
FACTS_MAX = 6 * 1024
DIGEST_MAX = 3 * 1024
DIGEST_S = 2 * 3600  # BOTDIGEST: no (or a future) digest.read -> the last 2 h
NEEDS_RE = re.compile(r"BLOCKED|needs[ _]orchestrator", re.I)
REPORT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
# BOTDIGEST: event kind -> the fields its digest row prints (sha fields cut to 10)
DIGEST_EVENTS = {
    "dispatch": ("lane", "id", "model", "effort"),
    "nudge": ("lane", "n"), "outage_nudge": ("lane", "n"), "budget_nudge": ("lane", "n"),
    "idle_nudge": ("lane", "n"),
    "respawn": ("lane", "id", "rc"), "redispatch": ("lane", "old_id", "id"),
    "blocked_attention": ("lane", "id", "nudges"), "resume_in_place": ("lane", "id"),
    "heal": ("lane", "was", "status"),
    "ci_result": ("lane", "sha", "result"), "ci_rerun": ("lane", "sha", "rc"),
    "push": ("sha", "ci_run"), "push_blocked": ("approval", "reason"), "fast_forward": ("sha",),
    "page": ("result",), "pace": ("band", "mode", "usage_pct"), "disk_hold": ("free_gb",),
    "outage_hold": ("why",), "outage_clear": (), "quota_reset": (), "breaker": ("state", "reason"),
}
ALIVE_S = 10 * 60  # status.json older than this = the conveyor is not writing (stale)
SECRET_RES = (  # facts.md never carries a URL (ntfy topic), a key or a key=value secret
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|xox[abp]-|AKIA)[\w-]{6,}"), "[key]"),
    (re.compile(r"(?i)\b(bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"""(?i)(?<![a-z0-9])(token|key|secret|password|passwd|topic|bearer|auth\w*)(["']?\s*=?[:=>]\s*)"""
                r"""(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|\S+)"""), r"\1\2[redacted]"),
    # escape-aware; an unclosed quote (cut head/cap) redacts to the end
    # long token between dashes, so push-<40 hex>-lane (approval names) and a hex sha never match
    (re.compile(r"(?=[\w+]*[g-zG-Z])(?=[\w+]*\d)[\w+]{32,}"), "[redacted]"),
)
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
COMMIT_RE = re.compile(r"(?m)^[ \t]*COMMIT:?(.*)$")
CI_RE = re.compile(r"(?m)^[ \t]*CI BRANCH:?[ \t]*(ci/[\w./-]+)")
FACT_REPORTS = 8
TRIM = ("reports", "attention", "slices", "approvals", "lanes")  # size cap: oldest report rows go first
ASK_MAX = {"title": 60, "why": 140, "step": 90, "where": 80, "cmd": 300}
OUTSIDE_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|~[\\/]|\\\\|%\w+%|\$env:|\$home\b|\.jarvis\b"
                        r"|(?<![\w\\/.])(?:\.\.[\\/]|[\\/](?:users|home|[a-z])[\\/]))\S*")  # never outside the repo
LANE_NAME_RE = re.compile(r"[A-Za-z0-9][\w.-]*")
OTHER_HOLD = {"ready", "running", "ci_pending", "overlap", "same_name_busy", "int_busy", "breaker_open"}
LIVE_STATES = {"working", "running", "starting", "blocked"}  # facts "running lanes": others are history
LANES_MAX = 10
CONVEYOR_PY = "scripts/conveyor.py"
RESTART_CMD = ("Stop-ScheduledTask -TaskName 'JARVIS Konveyor'; Start-Sleep 310; "
               "Start-ScheduledTask -TaskName 'JARVIS Konveyor'")
LIVE_KEYS = ("ack", "asks", "hold_dispatch", "page_salih")  # the live.json schema
# Only these can ever go live. page_salih stays out: the conveyor pager reads build/conveyor/pager.json
# only, which is outside what the bot may write; everything else in ACTIONS is never live.
LIVE_OK = ("ack", "asks", "hold_dispatch")
PAUSE_TEXT = "bot: "  # + rule; a PAUSE with other text is not the bot's and is never removed


def clean(line):
    """One facts.md line with no secret: cv.redacted (sk-ant-, the ntfy topic) then SECRET_RES."""
    line = cv.redacted(line, "", 400)
    for rx, repl in SECRET_RES:
        line = rx.sub(repl, line)
    return line


def ask_text(s, cap):
    """One ask string: no secret (clean), no path outside the repo, at most cap chars."""
    s = OUTSIDE_RE.sub("[path]", clean(str(s)))
    return s if len(s) <= cap else s[:cap - 1] + "…"


def git_ref(root, name):
    """A ref's sha read from <root>/.git without a git process (loose ref, then packed-refs), else None."""
    git = Path(root) / ".git"
    if not git.is_dir():
        return None
    try:
        loose = git / name
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8").strip()
            return sha if cv.SHA_RE.fullmatch(sha) else None
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == name and cv.SHA_RE.fullmatch(parts[0]):
                    return parts[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def wake_http(topic, text):
    req = urllib.request.Request("https://ntfy.sh/" + topic, data=text.encode("utf-8"), method="POST")
    urllib.request.urlopen(req, timeout=10).close()  # nosec B310 - fixed https URL


def proc_started(pid):
    """A process start time (epoch seconds) from its Windows FILETIME; OSError when unreadable."""
    return cv.WinProcs().created(pid) / 1e7 - 11644473600


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def stamp(p):
    """A file's before/after for a live decision line: "absent", or its mtime, size and first 80 bytes."""
    try:
        s = p.stat()
        head = p.read_bytes()[:80].decode("utf-8", errors="replace")
    except OSError:
        return "absent"
    return {"mtime": cv.iso(s.st_mtime), "bytes": s.st_size, "text": clean(head)}


def agree_key(action, target):
    """The target both sides name (lowercase), None for an action that is never matched."""
    t = str(target)
    if action == "ack":
        return "ack:" + cv.ack_name(t).lower()
    if action in KEY1:  # a lane, or a push_blocked approval name: its ci suffix is the lane
        m = cv.APPROVAL_RE.fullmatch(STAMP_RE.sub("", t))
        # the sha stays in the key: two pushes of one lane are two decisions, never one match
        return "key1:" + (f"{m.group(2) or ''}@{m.group(1)[:7]}" if m else t).lower()
    return {"queue_fix_slice": "fix:" + t.lower(), "rerun_ci_once": "rerun:" + t.lower(),
            "hold_dispatch": "dispatch", "page_salih": "salih"}.get(action)


def same_key(a, b):
    """Two agree keys name one target; a lane-less approval (push-<sha>, "key1:@sha7") matches its sha's lane."""
    if a == b:
        return True
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    for x, y in ((a, b), (b, a)):  # BOTFIX3D: a board ask names the bot ask's subject in its text
        if x.startswith("ask:board:") and y.startswith("ask:") and not y.startswith("ask:board:"):
            s = y[4:].split(":", 1)[-1]
            return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", x[10:])
                       for w in ASK_WORDS.get(s, (s,)) if w)
    return any(x.startswith("key1:@") and len(x) > 6 and y.startswith("key1:") and y.endswith(x[5:])
               for x, y in ((a, b), (b, a)))


def agree_blank():
    return {"version": AGREE_VERSION, "start": None, "since": None, "pending": [], "actual": [], "known": None,
            "answered": [], "totals": {}, "rules": {}, "key1": {"agree": 0, "total": 0}, "recent": [], "classes": {},
            "graduation": {"days": 0, "pushes": 0, "agree_pct": 0.0, "ready": False, "stage_a": False,
                           "classes": {}}}


def horizon(key):
    return DAY_S if key.split(":")[0] in ("ack", "ask", "key1", "fix") else MATCH_S


def klass(p, a):
    """The metric class of a verdict; key-1 splits by who wrote the approval."""
    k = (p or a)["key"].split(":")[0]
    if k == "key1":
        return "key1_conveyor" if a and a["side"] == "conveyor" else "key1_orch"
    return k if k in ("ack", "ask", "fix") else None


def entries(v):
    return isinstance(v, list) and all(
        isinstance(e, dict) and num(e.get("at")) and isinstance(e.get("key"), str) and e.get("action") in ACTIONS
        for e in v)


def counts(v):
    return isinstance(v, dict) and all(isinstance(c, dict) and all(type(n) is int for n in c.values())
                                       for c in v.values())


def known_ok(v):
    return v is None or isinstance(v, dict) and isinstance(v.get("acks"), dict) and \
        all(num(t) for t in v["acks"].values()) and \
        all(isinstance(v.get(k), list) and all(isinstance(x, str) for x in v[k]) for k in ("approvals", "backlog")) \
        and isinstance(v.get("pace"), dict) and all(num(x) or isinstance(x, bool) for x in v["pace"].values()) \
        and isinstance(v.get("board", []), list) and all(isinstance(x, str) for x in v.get("board", []))


AGREE_SHAPES = {  # shadow.json "agreement"; a present key of another shape resets the block (fail closed)
    "start": lambda v: v is None or num(v),
    "since": lambda v: v is None or isinstance(v, str),
    "pending": lambda v: entries(v) and all(isinstance(e.get("rule"), str) for e in v),
    "actual": lambda v: entries(v) and all(e.get("side") in ("orchestrator", "conveyor", "salih") for e in v),
    "known": known_ok,
    "answered": entries,
    "totals": counts,
    "rules": counts,
    "key1": lambda v: isinstance(v, dict) and all(type(v.get(k)) is int for k in ("agree", "total")),
    "recent": lambda v: isinstance(v, list) and all(isinstance(r, dict) for r in v),
    "graduation": lambda v: isinstance(v, dict),
    "version": lambda v: type(v) is int,
    "classes": counts,
}


def routine(iid, reason):
    """O33: an attention item the bot may ack - a cleanup/preserved item, or every '; ' part routine."""
    return iid.startswith(("cleanup:", "preserved:")) or \
        bool(reason and all(ROUTINE_RE.match(p) for p in reason.split("; ")))


def rows(*fields):  # a dict of dicts whose `fields` are numbers
    return lambda v: isinstance(v, dict) and all(isinstance(x, dict) and all(num(x.get(f)) for f in fields)
                                                 for x in v.values())


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
    "asks": lambda v: isinstance(v, dict) and all(isinstance(x, str) for x in v.values()),
    "conv_push": lambda v: isinstance(v, dict) and num(v.get("at", 0)) and isinstance(v.get("sha", ""), str),
    "live": lambda v: isinstance(v, dict) and all(isinstance(v.get(k, {}), dict) and all(num(x) for x in v.get(k, {}).values())
                                                  for k in ("demoted", "acks"))
    and isinstance(v.get("pause", {}), dict) and isinstance(v.get("pause", {}).get("text", ""), str)
    and num(v.get("pause", {}).get("mtime", 0)) and isinstance(v.get("asks", False), bool),
    "blocked": lambda v: isinstance(v, dict) and all(num(x) for x in v.values()),
    "items": rows("since", "seen"),
    "auto": rows("at"),
    "waiting": rows("at"),
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
    def __init__(self, root, now=time.time, sleep=time.sleep, quota_ceiling=90, from_start=False,
                 started=proc_started, wake_post=None, wake_topic=None):
        self.root = Path(root)
        self.wake_post, self.wake_topic = wake_post, wake_topic  # None: wake_http / WAKE_TOPIC at post time
        self.wakes = []  # the replay calls check_attention without run()
        self.started = started  # pid -> process start epoch (OSError when unreadable)
        self.conv = self.root / "build" / "conveyor"
        self.out = self.root / "build" / "bot"
        self.now, self.sleep, self.ceiling, self.from_start = now, sleep, quota_ceiling, from_start
        self.live = set()  # BOTLIVE: only run() loads live.json; the replay drives decide() and stays shadow

    # ---- state -------------------------------------------------------------------------------
    def run(self):
        """One pass; returns the decisions it appended."""
        self.out.mkdir(parents=True, exist_ok=True)
        self.new, self.reports, self.verdicts, self.observed, self.wakes = [], {}, [], [], []
        self.deferred = set()  # BOTFIX3E: ack keys observe() deferred to the next pass
        self.live, self.attn = set(), None  # attn: ack names on status.json's attention list this pass
        self.st = self.load_state()
        try:
            self.live = self.load_live()
        except Exception as e:  # RecursionError on deep nesting too: fail closed to shadow
            self.live = set()
            self.bad("live.json", e)
        self.check_agreement()
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
        saved = json.loads(json.dumps(self.st["agreement"]))
        try:
            self.agree()
        except Exception as e:  # fail closed: the block stays as it was, the decisions still land
            saved["actual"] += self.observed  # the event cursor already moved past them
            self.st["agreement"], self.verdicts = saved, []
            if self.fresh("bad:agreement", self.now()):
                self.decide(self.now(), "agreement", "escalate_orchestrator", "FAILCLOSED", "orchbot",
                            f"agreement error: {e!r}"[:300])
        try:
            if not self.retry(self.write_facts):
                raise OSError(f"write failed after {cv.REPLACE_TRIES} tries")
        except Exception as e:  # the old facts.md stays; one decision per episode, the pass goes on
            if self.fresh("bad:facts", self.now()):
                self.decide(self.now(), "facts.md write failed", "escalate_orchestrator", "FAILCLOSED",
                            "facts.md", f"{e!r}"[:300])
        try:
            self.release_pause()
        except Exception as e:  # the PAUSE stays (the safe side); one decision per episode
            if self.fresh("bad:pause", self.now()):
                self.decide(self.now(), "PAUSE release failed", "escalate_orchestrator", "FAILCLOSED", "PAUSE",
                            f"{e!r}"[:300])
        try:
            asks = self.asks()
            if not self.retry(self.write_asks, asks):
                raise OSError(f"write failed after {cv.REPLACE_TRIES} tries")
            self.log_asks(asks)
            live, on = self.st.setdefault("live", {}), "asks" in self.live
            if on != bool(live.get("asks")):
                if on:
                    live["asks"] = True
                else:
                    live.pop("asks", None)
                self.live_rec({"saw": "live.json asks", "rule": "LIVE", "target": "asks", "detail": ""},
                              "build/bot/asks.json", "live" if not on else "shadow", "live" if on else "shadow",
                              f"set asks {str(not on).lower()} in build/bot/live.json", did="mark" if on else "unmark")
        except Exception as e:  # the old asks.json stays and so does the open set; the pass goes on
            if self.fresh("bad:asks", self.now()):
                self.decide(self.now(), "asks.json write failed", "escalate_orchestrator", "FAILCLOSED",
                            "asks.json", f"{e!r}"[:300])
        try:  # last, so it lists this pass's decisions too
            if not self.retry(self.write_digest):
                raise OSError(f"write failed after {cv.REPLACE_TRIES} tries")
        except Exception as e:  # the old digest.md stays; one decision per episode, the pass goes on
            if self.fresh("bad:digest", self.now()):
                self.decide(self.now(), "digest.md write failed", "escalate_orchestrator", "FAILCLOSED",
                            "digest.md", f"{e!r}"[:300])
        try:
            self.wake()
        except Exception as e:  # never break the tick; never the topic or the message in the log
            print(f"orchbot: orch-wake topic step failed: {type(e).__name__}", file=sys.stderr)
        self.save()
        return self.new

    def wake(self):
        """BOTFIX3: queue each new wake item once per target episode; post one coalesced message per WAKE_S."""
        now, w = self.now(), self.st.get("wake")
        if not (isinstance(w, dict) and isinstance(w.get("sent", {}), dict) and num(w.get("last", 0))
                and isinstance(w.get("queue", []), list) and all(isinstance(q, str) for q in w.get("queue", []))):
            w = self.st["wake"] = {}  # a bad block starts over (fail closed: at worst one repeat message)
        # a non-number, future or non-finite time is dropped (comparisons with nan/inf are false)
        sent = w["sent"] = {k: v for k, v in w.get("sent", {}).items() if num(v) and now - SEEN_S <= v <= now}
        queue = w.setdefault("queue", [])
        items = [(d["rule"], d.get("target", ""), d["would_do"]) for d in self.new  # live records: no would_do
                 if d.get("rule") in WAKE and d.get("would_do", "none") != "none"] + self.wakes
        for rule, target, action in items:
            key = f"{rule}:{target}"
            last = sent.get(key)
            sent[key] = now
            # last path segment only, cut at query/fragment/whitespace: no paths, URL secrets or extra lines
            seg = re.split(r"[?#\s]", str(target).replace("\\", "/"))[0].rstrip("/").rsplit("/", 1)[-1]
            seg = re.sub(r"[^\w.:@-]", "_", seg)[:60]
            line = f"{rule} {seg} {action}"
            if (last is None or now - last > EPISODE_S) and line not in queue and len(queue) < 50:
                queue.append(line)  # a full queue drops the newest, never the oldest unsent
        if (self.out / "WAKE_OFF").exists():  # kill switch: nothing queued is sent later either
            queue.clear()
            return
        if not queue or 0 <= now - w.get("last", 0) < WAKE_S:  # a future or inf last never mutes
            return
        w["last"] = now
        try:
            path = Path(self.wake_topic or WAKE_TOPIC)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
            topic = path.read_text(encoding="utf-8-sig").strip()
            if not TOPIC_RE.fullmatch(topic) or topic == os.environ.get("JARVIS_NTFY_TOPIC", "").strip():
                raise ValueError("bad topic")  # never the human paging topic
            (self.wake_post or wake_http)(topic, "\n".join(queue[:20]))
        except Exception as e:  # the queue stays for the next window
            print(f"orchbot: orch-wake topic post failed: {type(e).__name__}", file=sys.stderr)
            return
        del queue[:20]

    def load_state(self):
        blank = {"cursor": None, "decisions": [], "counters": {}, "seen": {}, "history": [],
                 "fixes": {}, "respawns": {}, "head": "", "agreement": agree_blank(),
                 "asks": {}, "conv_push": {}, "live": {}, "wake": {}, "hold_since": {},
                 "blocked": {}, "items": {}, "auto": {}, "waiting": {}}
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
            if (self.out / "live.json").exists():
                # DECISION TAKEN: a state reset demotes every live action (alternative: keep them live);
                # PAUSE ownership is lost with the state, so a bot PAUSE then waits for a human
                blank["live"] = {"demoted": {k: self.now() for k in LIVE_OK}}
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

    def decide(self, at, saw, action, rule, target, detail="", key=None):
        assert action in ACTIONS, action  # nosec B101 - a programming error, the closed enum
        orig = action
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
        key = key or agree_key(action, target)
        if key:
            self.st["agreement"]["pending"].append({"at": at, "action": action, "rule": rule, "target": target,
                                                    "key": key, "detail": rec["detail"]})
        # BOTLIVE: only the LIVE_OK file actions; every other action stays a log line whatever live.json says
        if orig in self.live and action != orig:  # the loop guard tripped: back to shadow until reset
            self.demote(orig, rec["detail"], "LOOP")
        elif action in self.live and action in ("ack", "hold_dispatch"):
            try:
                self.act_live(rec)
            except (OSError, ValueError) as e:
                self.demote(action, f"{e!r}"[:300], "FAILCLOSED")
                self.decide(at, f"live {action} failed", "escalate_orchestrator", "FAILCLOSED", "live",
                            f"{e!r}"[:300])

    # ---- live (BOTLIVE) ----------------------------------------------------------------------
    def load_live(self):
        """The live actions for this pass: OFF wins, a malformed live.json is all shadow, a demoted
        action stays shadow until its live.demoted entry is deleted from shadow.json."""
        if (self.out / "OFF").exists():
            return set()
        cfg = self.read_json(self.out / "live.json", {})
        bad = sorted(str(k) for k, v in cfg.items() if k not in LIVE_KEYS or type(v) is not bool)
        if bad:
            self.bad("live.json", ValueError(f"unknown key or not a bool: {', '.join(bad)}"[:200]))
            return set()
        if cfg.get("page_salih") and self.fresh("live:page_salih", self.now()):
            self.decide(self.now(), "live.json page_salih", "escalate_orchestrator", "LIVE", "live.json",
                        "page_salih cannot go live: the pager reads only build/conveyor/pager.json")
        demoted = self.st.get("live", {}).get("demoted", {})
        return {k for k in LIVE_OK if cfg.get(k) is True and k not in demoted}

    def live_rec(self, rec, path, before, after, undo, did=None):
        """One decisions.jsonl line per live action (mode "live"); not counted, not matched for agreement."""
        self.new.append({"ts": cv.iso(self.now()), "mode": "live", "saw": rec["saw"], "did": did or rec.get("would_do"),
                         "rule": rec["rule"], "target": rec["target"], "file": path, "before": before,
                         "after": after, "undo": undo, "detail": rec["detail"]})

    def act_live(self, rec):
        live = self.st.setdefault("live", {})
        if rec["would_do"] == "ack":
            name = cv.ack_name(rec["target"])
            if not name or name.startswith(".") or any(c in name for c in "/\\:"):
                raise ValueError(f"unsafe ack name {name!r}")
            p = self.conv / "ack" / name
            before = stamp(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            os.utime(p, (self.now(),) * 2)
            live.setdefault("acks", {})[name] = p.stat().st_mtime
            self.live_rec(rec, f"build/conveyor/ack/{name}", before, stamp(p),
                          f"delete build/conveyor/ack/{name}" if before == "absent"
                          else f"set its mtime back to {before['mtime']}")
            return
        p, owned = self.conv / "PAUSE", live.get("pause")
        if p.exists():  # a PAUSE is never rewritten; one the bot did not create is never the bot's
            self.live_rec(rec, "build/conveyor/PAUSE", stamp(p), stamp(p),
                          "delete build/conveyor/PAUSE" if owned else "none: a PAUSE the bot did not create is kept",
                          did="kept")
            return
        text = PAUSE_TEXT + rec["rule"]
        with open(p, "x", encoding="utf-8") as f:
            f.write(text)
        live["pause"] = {"text": text, "mtime": p.stat().st_mtime, "at": self.now()}
        self.live_rec(rec, "build/conveyor/PAUSE", "absent", stamp(p), "delete build/conveyor/PAUSE")

    def release_pause(self):
        """Removes the bot's own PAUSE once its quota/outage episode is over; a PAUSE whose text or mtime
        changed is no longer the bot's and is never removed."""
        live = self.st.get("live", {})
        owned = live.get("pause")
        if not owned:
            return
        p = self.conv / "PAUSE"
        try:
            text, mtime = p.read_text(encoding="utf-8"), p.stat().st_mtime
        except FileNotFoundError:
            text = mtime = None
        if text is None or text != owned.get("text") or mtime != owned.get("mtime"):
            live.pop("pause")
            return
        # DECISION TAKEN: release only while hold_dispatch is live (alternative: always release it)
        if "hold_dispatch" not in self.live or any(
                self.now() - self.st["seen"].get(k, -EPISODE_S - 1) <= EPISODE_S for k in ("quota", "outage")):
            return
        before = stamp(p)
        p.unlink()
        live.pop("pause")
        self.live_rec({"saw": "quota/outage episode over", "rule": "LIVE", "target": "dispatch", "detail": ""},
                      "build/conveyor/PAUSE", before, "absent", f"recreate build/conveyor/PAUSE with {text!r}",
                      did="unpause")

    def demote(self, action, why, rule):
        self.st.setdefault("live", {}).setdefault("demoted", {})[action] = self.now()
        self.live.discard(action)
        self.live_rec({"saw": f"live {action}", "rule": rule, "target": action, "detail": why},
                      "build/bot/shadow.json", "live", "shadow",
                      f"delete live.demoted.{action} in build/bot/shadow.json", did="demote")

    def save(self):
        now = self.now()
        st = self.st
        st["seen"] = {k: v for k, v in st["seen"].items() if v >= now - SEEN_S}
        st["blocked"] = {k: v for k, v in st["blocked"].items() if v >= now - SEEN_S}
        for book, f in (("items", "seen"), ("auto", "at"), ("waiting", "at")):
            st[book] = {k: v for k, v in st[book].items() if v[f] >= now - SEEN_S}
        st["respawns"] = {n: ts for n, ts in st["respawns"].items() if ts and ts[-1] >= now - RESPAWN_S}
        st["history"] = [h for h in st["history"] if h[0] >= now - LOOP_S]
        st["counters"]["passes"] = st["counters"].get("passes", 0) + 1
        if (self.new or self.verdicts) and not self.retry(self.append, self.new + self.verdicts):
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
        st = dict(self.st, decisions=(self.st["decisions"] + self.new)[-KEEP:], updated=cv.iso(self.now()),
                  asks_open=len(self.st["asks"]))
        if not st.get("live"):  # shadow-only output stays as it was before BOTLIVE
            st.pop("live", None)
        path = self.out / "shadow.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ---- asks (BOTASKS) ----------------------------------------------------------------------
    def write_asks(self, asks):
        """asks.json via tmp + os.replace, the build/pano/build.py ask shape plus id/since/source."""
        path = self.out / "asks.json"
        tmp = path.with_name(path.name + ".tmp")
        body = {"updated": cv.iso(self.now()), "asks": asks}
        if "asks" in self.live:  # BOTLIVE: marked for the pano
            body = {"updated": body["updated"], "live": True, "asks": [dict(a, pano=True) for a in asks]}
        tmp.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def log_asks(self, asks):
        """One decision when an ask opens and one when it closes; st["asks"] is the open set."""
        now, was = self.now(), self.st["asks"]
        cur = {a["id"]: a["since"] for a in asks}
        open_ = {p["key"] for p in self.st["agreement"]["pending"]}
        for i in sorted(cur.keys() - was.keys()):
            # BOTFIX3D: a flapping ask (closed one pass, reopened) is still the one pending decision
            k = f"ask:{i}".lower()
            self.decide(now, f"ask {i}", "none", "ASK", i, f"open {i}", key=k if k not in open_ else None)
        for i in sorted(was.keys() - cur.keys()):
            # BOTFIX3D: the bot's own ask closing is no actual; observe() reads the board and Salih's files
            self.decide(now, f"ask {i}", "none", "ASK", i, f"close {i}")
        self.st["asks"] = cur

    def asks(self):
        """What Salih would be asked this pass (SALIH RULE O39), derived from files, never acted on.
        A case whose input is unknown or malformed gives no ask and one escalate per episode."""
        self.status = self.read_json(self.conv / "status.json", {})
        out = []
        for case in (self.ask_key1, self.ask_restart, self.ask_t2):
            try:
                out += case()
            except Exception as e:
                if self.fresh(f"bad:{case.__name__}", self.now()):
                    self.decide(self.now(), case.__name__, "escalate_orchestrator", "FAILCLOSED", "asks.json",
                                f"{e!r}"[:300])
        return out

    def ask(self, aid, rule, files, title, blocks, why, steps, cmds, then, time="1 dk",
            where="PowerShell, JARVIS depo kökü"):
        c = ASK_MAX
        for _, t in cmds:  # a pasted cmd names the approval file: a cut or rewritten one is wrong
            if ask_text(t, c["cmd"]) != t:
                raise ValueError(f"{aid}: cmd would be truncated or rewritten, no ask (fail closed)")
        safe = ask_text(aid, c["where"])
        if safe != aid:  # two raw ids must never collapse into one sanitized id
            safe = ask_text(aid, c["where"] - 9) + "-" + hashlib.sha1(str(aid).encode(), usedforsecurity=False).hexdigest()[:8]
        aid = safe
        return {"id": aid, "title": ask_text(title, c["title"]), "blocks": ask_text(blocks, c["why"]),
                "where": ask_text(where, c["where"]), "time": time, "why": ask_text(why, c["why"]),
                "steps": [ask_text(s, c["step"]) for s in steps[:3]],
                "cmds": [{"label": ask_text(lb, c["title"]), "text": ask_text(t, c["cmd"])} for lb, t in cmds],
                "then": ask_text(then, c["why"]), "since": self.st["asks"].get(aid) or cv.iso(self.now()),
                "source": {"rule": ask_text(rule, c["title"]), "files": [ask_text(f, c["where"]) for f in files]}}

    def ask_key1(self):
        """Key 1: a GREEN / CI_PENDING candidate whose range touches CONVSAFE paths, no approval file."""
        if not isinstance(self.lanes, dict):
            raise ValueError("lanes.json is not an object")
        out = []
        for lane, ln in sorted(self.lanes.items()):
            if not isinstance(ln, dict) or not LANE_NAME_RE.fullmatch(lane):  # lane goes into a pasted cmd
                raise ValueError(f"lanes.json {lane!r}: not an object or not a plain lane name")
            ci, status = ln.get("ci") or {}, str(ln.get("status") or "")
            if status not in ("GREEN", cv.CI_PENDING) or not isinstance(ci, dict) or not ci.get("sha"):
                continue
            sha = str(ci["sha"])
            if not cv.SHA_RE.fullmatch(sha):
                raise ValueError(f"lanes.json {lane}: ci.sha is not a sha")
            rep = self.report(lane, ln)
            if not (ci.get("cand") or cv.CANDIDATE_RE.search(rep)) or self.approved(sha, done=True):
                continue
            files = self.changed(ln, rep)
            if not files:
                raise ValueError(f"{lane}: no changed-file list for the range")
            why = protected(files)
            if not why:
                continue
            out.append(self.ask(
                f"key1:{lane}", "CONVSAFE",
                ["build/conveyor/lanes.json", f"build/reports/{lane}.md", "build/approvals"],
                f"Anahtar 1: {lane} push onayı", f"{lane} aralığının main'e push'u",
                f"{lane} CI {status}; aralık korunan yola dokunuyor: {why}",
                [f"Aralığı oku: build/reports/{lane}.md", "Uygunsa aşağıdaki komutla onay dosyasını oluştur",
                 "Push'u konveyör kendi kapısından yapar"],
                [("Anahtar 1", f"New-Item -ItemType File build\\approvals\\push-{sha}-{lane}")],
                "Konveyör kontrolleri geçerse main'e push eder; bu istek kapanır."))
        return out

    def seen_push(self, ev, at, lane):
        """A push whose lane's range changed scripts/conveyor.py: the running conveyor is now old."""
        sha = str(ev.get("sha") or "")
        m = cv.APPROVAL_RE.fullmatch(STAMP_RE.sub("", str(ev.get("approval") or "")))
        lane = (m.group(2) if m else None) or lane
        # approval names are lowercase (push-<sha>-int63b), lanes.json keys are not (INT63B)
        lane = next((k for k in self.lanes if k.lower() == lane.lower()), lane) if isinstance(self.lanes, dict) else lane
        ln = self.lanes.get(lane) if isinstance(self.lanes, dict) else None
        if not isinstance(ln, dict):
            return self.decide(at, f"push {sha[:12]}", "escalate_orchestrator", "FAILCLOSED", "asks.json",
                               f"push of unknown lane {lane!r}: cannot tell whether {CONVEYOR_PY} changed")
        # ./ prefix, case, :line suffix and glob grants all count (fail closed: a needless ask, not a miss)
        if any(fnmatch.fnmatchcase(CONVEYOR_PY, re.sub(r":\d+$", "", f.replace("\\", "/").lower()).removeprefix("./"))
               for f in self.changed(ln, self.report(lane, ln))):
            self.st["conv_push"] = {"sha": sha, "at": at, "lane": lane}

    def ask_restart(self):
        """Konveyör yeniden başlat: a pushed conveyor.py change and a conveyor process older than it.
        DECISION TAKEN: the start is the OS creation time of status.json's pid (the lock carries only
        a heartbeat); a start at/after the push event closes the ask, an unreadable start keeps the
        push pending and escalates once (fail closed: never assume a restart)."""
        pid, push = self.status.get("pid"), self.st["conv_push"]
        if not push:
            return []
        if type(pid) is not int or not 0 < pid < 2 ** 32:
            raise ValueError("status.json has no pid: the conveyor start is unknown")
        start = self.started(int(pid))
        if not num(start):
            raise ValueError(f"conveyor pid {pid}: start time unreadable")
        if start >= push["at"]:
            # a stale status.json may name a dead pid that the OS reused: only a fresh one closes the ask
            upd = parse_ts(self.status.get("updated_at"))
            if upd is None or not 0 <= self.now() - upd <= ALIVE_S:
                raise ValueError(f"status.json not fresh (updated_at {self.status.get('updated_at')!r}): "
                                 f"pid {pid} may be reused; the push stays pending")
            self.st["conv_push"] = {}
            return []
        return [self.ask(
            "restart", "O39", ["build/conveyor/events.jsonl", "build/conveyor/status.json",
                               "build/conveyor/lanes.json"],
            "Konveyörü yeniden başlat", "Yeni konveyör kodunun çalışması",
            f"{push.get('lane', '?')} push'u {CONVEYOR_PY} dosyasını değiştirdi; çalışan konveyör "
            f"o push'tan ({str(push.get('sha'))[:12]}) eski.",
            ["Aşağıdaki komutu yönetici PowerShell'de çalıştır",
             "310 sn bekleme kilidin bayatlaması içindir, kesme"],
            [("Yeniden başlat", RESTART_CMD)],
            "Konveyör yeni kodla açılır; yeni pid görülünce bu istek kapanır.", time="6 dk")]

    def lane_finished(self, lane):
        """Either conveyor snapshot can prove a stale working lane has finished."""
        name = lane.get("id") or lane.get("name")
        records = (lane, getattr(self, "lanes", {}).get(name, {}))
        return any(isinstance(record, dict) and (
            record.get("finished") is True or str(record.get("status") or "").upper() in {
                "PASS", "BLOCKED", "DROPPED", "STUCK", "MISSING", "GREEN", "CI_PENDING", "DONE"})
                   for record in records)

    def ask_t2(self):
        """Onay: a T2 slice with approved=false that is the only thing keeping the queue dry."""
        items, lanes = self.status.get("backlog"), self.status.get("lanes")  # missing lanes = unknown, not idle
        if items is None:
            return []
        if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("id"), str)
                                                  for i in items) or not isinstance(lanes, list):
            raise ValueError("status.json backlog/lanes malformed")
        states = {str(i.get("state")) for i in items}
        # anything else holding or busy (conveyor pick_slice states, pause, holds): T2 is not the only cause
        if states & OTHER_HOLD or any(s.startswith("held: ") for s in states) or any(
                self.status.get(k) for k in ("paused", "usage_hold", "memory_hold", "outage_hold")) or any(not isinstance(x, dict) or not isinstance(x.get("state"), (str, type(None)))
                                                    or (str(x.get("state")).lower() in LIVE_STATES and not self.lane_finished(x))
                                                for x in lanes):  # an unreadable lane is never idle
            return []
        return [self.ask(
            f"t2:{i['id']}", "T2", ["build/conveyor/status.json", "build/conveyor/backlog.json"],
            f"Onay: {i['id']} (T2)", "Kuyruk: başka hazır dilim yok",
            f"{i['id']} T2 dilimi onaysız (approved=false); kuyrukta başka hazır iş yok, konveyör boşta.",
            [f"{i['id']} kapsamını build/conveyor/backlog.json içinde oku",
             "Uygunsa o dilimde approved: true yap ve kaydet",
             "Uygun değilse kararı orkestratöre yaz"],
            [("Backlog'u aç", "notepad build\\conveyor\\backlog.json")],
            "Konveyör dilimi bir sonraki turda başlatır; bu istek kapanır.", time="5 dk")
            for i in items if i.get("state") == "needs_t2_approval"]

    # ---- facts (BOTFACTS) --------------------------------------------------------------------
    def write_facts(self):
        """facts.md via tmp + os.replace: the old file stays whole until the new one is complete."""
        path = self.out / "facts.md"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.facts(), encoding="utf-8")
        os.replace(tmp, path)

    def quiet(self, path, kind=dict):
        """The JSON at path when it is a `kind`, else None. No decision: facts.md prints unknown."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, kind) else None

    def facts(self):
        """The facts.md text: fixed section order, every line ends with its source file; a missing
        or unreadable source prints "unknown (<file>)", never a guess. Over FACTS_MAX the oldest
        list rows go first (reports, then attention, slices, approvals, lanes)."""
        now = self.now()
        st = self.quiet(self.conv / "status.json")
        pace = self.quiet(self.conv / "pace.json")
        backlog = self.quiet(self.conv / "backlog.json", (dict, list))
        S = "status.json"

        def unk(f):
            return f"unknown ({f})"

        def yn(v):
            return "yes" if v else "no"

        def get(k, ok):
            return st[k] if st is not None and ok(st.get(k)) else None

        # status.json publishes no main sha: read the refs from .git files (no git process)
        main_lines = [f"{k}: {v[:10]} (.git)" if v else f"{k}: {unk('.git')}"
                      for k, v in (("local", git_ref(self.root, "refs/heads/main")),
                                   ("origin", git_ref(self.root, "refs/remotes/origin/main")))]

        conv = []
        upd = parse_ts(get("updated_at", lambda v: isinstance(v, str)))
        if upd is None:
            conv.append(f"alive: {unk(S)}")
        else:
            age = int(now - upd)
            conv.append(f"alive: {'yes' if age <= ALIVE_S else 'no (stale)'}, status written {age // 60} min ago ({S})")
        paused = get("paused", lambda v: isinstance(v, bool))
        conv.append(f"paused: {yn(paused)} ({S})" if paused is not None else f"paused: {unk(S)}")
        band = get("band", lambda v: isinstance(v, list))
        conv.append(f"band: {', '.join(map(str, band)) or 'none'} ({S})" if band is not None else f"band: {unk(S)}")

        quota = []
        pct, hold = get("usage_pct", num), get("usage_hold", lambda v: isinstance(v, bool))
        quota.append(f"usage: {pct:g}% ({S})" if pct is not None else f"usage: {unk(S)}")
        quota.append(f"usage hold: {yn(hold)} ({S})" if hold is not None else f"usage hold: {unk(S)}")
        reset = None
        days = pace.get("days", cv.PACE_DEFAULTS["days"]) if pace is not None else None
        if num(days) and isinstance(pace.get("window_start"), str):
            try:
                reset = cv.iso(datetime.fromisoformat(pace["window_start"]).timestamp() + days * 86400)
            except (ValueError, OverflowError, OSError):
                reset = None
        quota.append(f"window reset: {reset} (pace.json)" if reset else f"window reset: {unk('pace.json')}")
        mhold = get("memory_hold", lambda v: isinstance(v, bool))
        free = (get("memory", lambda v: isinstance(v, dict)) or {}).get("free_gb")
        quota.append(f"memory hold: {yn(mhold)}{f', free {free:g} GB' if num(free) else ''} ({S})"
                     if mhold is not None else f"memory hold: {unk(S)}")

        lanes = get("lanes", lambda v: isinstance(v, list))
        lane_rows = []
        live = [ln for ln in lanes or [] if isinstance(ln, dict) and str(ln.get("state")).lower() in LIVE_STATES
                and not self.lane_finished(ln)]
        for ln in live[:LANES_MAX]:
            t = parse_ts(ln.get("started_at"))
            mins = f"{int(now - t) // 60} min" if t is not None else "unknown min"
            lane_rows.append(f"{ln.get('id') or ln.get('name')}: {ln.get('model')}/{ln.get('effort')}, "
                             f"running {mins}, state {ln.get('state')} ({S})")
        if len(live) > LANES_MAX:
            lane_rows.append(f"+{len(live) - LANES_MAX} more ({S})")

        raw = backlog.get("slices") if isinstance(backlog, dict) else backlog
        slices = {str(s.get("id")): s for s in raw if isinstance(s, dict)} if isinstance(raw, list) else None
        queue = get("backlog", lambda v: isinstance(v, list))
        slice_rows = []
        for b in [b for b in queue or [] if isinstance(b, dict) and b.get("id")][:5]:
            state = str(b.get("state"))
            if state != "waiting_deps":
                slice_rows.append(f"{b['id']}: {state} ({S})")
            elif slices is None:
                slice_rows.append(f"{b['id']}: waits on {unk('backlog.json')}")
            else:
                deps = (slices.get(str(b["id"])) or {}).get("depends_on") or []
                deps = ", ".join(map(str, deps)) if isinstance(deps, list) else str(deps)
                slice_rows.append(f"{b['id']}: waits on {deps or 'unknown'} ({S} + backlog.json)")

        report_rows = self.fact_reports()
        approval_rows = self.fact_approvals()
        breaker = get("breaker", lambda v: isinstance(v, str))
        gate = [f"push gate breaker: {breaker} ({S})" if breaker is not None else f"push gate breaker: {unk(S)}"]

        att = get("attention", lambda v: isinstance(v, list))
        att_rows = [f"{a.get('id')}: {str(a.get('reason'))[:120]} since {a.get('since')} ({S})"
                    for a in att or [] if isinstance(a, dict)]

        g = self.st.get("agreement", {}).get("graduation")
        if isinstance(g, dict) and all(num(g.get(k)) for k in ("days", "pushes", "agree_pct")):
            grad = [f"days {g['days']:g}/{GRAD_DAYS}, key-1 pushes {g['pushes']:g}/{GRAD_PUSHES}, "
                    f"agree {g['agree_pct']:g}%, ready {yn(g.get('ready') is True)} (shadow.json)"]
            if isinstance(g.get("classes"), dict):
                grad.append(f"stage A ready {yn(g.get('stage_a') is True)} (shadow.json)")
                grad += [f"{c}: n {v['n']:g}, agree {v['pct']:g}% (shadow.json)" for c, v in g["classes"].items()
                         if isinstance(v, dict) and num(v.get("n")) and num(v.get("pct"))]
        else:
            grad = [f"graduation: {unk('shadow.json')}"]

        none = {"lanes": f"none ({S})", "slices": f"none ({S})", "approvals": "none (build/approvals)",
                "attention": f"none ({S})", "reports": "none (build/reports)"}
        # (title, fixed lines, trimmable rows or None when the source is unknown, key)
        secs = [("main", main_lines, [], None), ("conveyor", conv, [], None), ("quota", quota, [], None),
                ("running lanes", [], lane_rows if lanes is not None else None, "lanes"),
                ("next slices", [], slice_rows if queue is not None else None, "slices"),
                ("last reports", [], report_rows, "reports"),
                ("approvals and push gate", gate, approval_rows, "approvals"),
                ("attention", [], att_rows if att is not None else None, "attention"),
                ("bot graduation", grad, [], None)]
        unknown = {"lanes": unk(S), "slices": unk(S), "approvals": unk("build/approvals"),
                   "attention": unk(S), "reports": unk("build/reports")}
        cut = dict.fromkeys(none, 0)

        def render():
            out = [f"# JARVIS facts at {cv.iso(now)} (scripts/orchbot.py)",
                   "Written by code from the files named on each line; unknown means the file was missing "
                   "or unreadable (scripts/orchbot.py)"]
            for title, fixed, rows, key in secs:
                out += ["", f"## {title}"] + [f"- {x}" for x in fixed]
                if key:
                    if rows is None:
                        out.append(f"- {unknown[key]}")
                    elif rows or cut[key]:
                        out += [f"- {x}" for x in rows]
                    else:
                        out.append(f"- {none[key]}")
                    if cut[key]:
                        out.append(f"- ... {cut[key]} more rows not shown (size cap, scripts/orchbot.py)")
            return "\n".join(clean(x) for x in out) + "\n"

        text = render()
        for title, fixed, rows, key in sorted((s for s in secs if s[3]), key=lambda s: TRIM.index(s[3])):
            while rows and len(text.encode("utf-8")) > FACTS_MAX:
                rows.pop()  # newest first, so the last row is the oldest
                cut[key] += 1
                text = render()
        # ponytail: byte cut as the last resort, only reachable with absurd fixed lines
        return text if len(text.encode("utf-8")) <= FACTS_MAX else \
            text.encode("utf-8")[:FACTS_MAX - 1].decode("utf-8", "ignore") + "\n"

    def fact_reports(self):
        """The last FACT_REPORTS reports by mtime, newest first: STATUS, COMMIT, CANDIDATE SHA, CI BRANCH."""
        d = self.root / "build" / "reports"
        try:
            paths = sorted((p for p in d.iterdir() if p.suffix == ".md" and p.is_file()),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:FACT_REPORTS]
        except OSError:
            return None
        rows = []
        for p in paths:
            src = f"build/reports/{p.name}"
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    rep = f.read(REPORT_MAX)
            except OSError:
                rows.append(f"{p.stem}: {f'unknown ({src})'}")
                continue
            first = rep.split("\n", 1)[0].strip()
            parts = [first[len("STATUS:"):].strip() if first.startswith("STATUS:") else "STATUS unknown"]
            for label, rx in (("commit", COMMIT_RE), ("candidate", cv.CANDIDATE_RE)):
                m = rx.search(rep)
                h = HEX_RE.search(m.group(1)) if m else None
                if h:
                    parts.append(f"{label} {h.group(0)[:10]}")
            m = CI_RE.search(rep)
            if m:
                parts.append(f"ci {m.group(1)}")
            rows.append(f"{p.stem}: {', '.join(parts)} ({src})")
        return rows

    def fact_approvals(self):
        """Pending approval files (build/approvals, not done/)."""
        try:
            names = sorted(n for n in os.listdir(self.root / "build" / "approvals")
                           if cv.APPROVAL_RE.fullmatch(n) and (self.root / "build" / "approvals" / n).is_file())
        except OSError:
            return None
        return [f"{n} pending (build/approvals)" for n in names]

    # ---- digest (BOTDIGEST) ------------------------------------------------------------------
    def write_digest(self):
        """digest.md via tmp + os.replace, like facts.md."""
        path = self.out / "digest.md"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.digest(), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def jsonl_tail(path):
        """The JSON-object lines of the last REPORT_MAX bytes of path; [] when unreadable."""
        try:
            with open(path, "rb") as f:
                start = max(0, f.seek(0, 2) - REPORT_MAX)
                f.seek(start)
                raw = f.read().split(b"\n")
        except OSError:
            return []
        out = []
        for b in raw[1:] if start else raw:
            try:
                rec = json.loads(b)
            except (ValueError, RecursionError):  # the cursor reader already escalates these lines
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def digest(self):
        """The digest.md text: what changed since the orchestrator touched build/bot/digest.read
        (else the last 2 h), oldest first, every line names its source. Over DIGEST_MAX the oldest
        rows go first."""
        now = self.now()
        try:
            read_at = (self.out / "digest.read").stat().st_mtime
        except OSError:
            read_at = None
        if read_at is not None and read_at <= now:
            since, src = read_at, "(build/bot/digest.read)"
        else:  # DECISION TAKEN: a future digest.read counts as absent -> the last 2 h
            since, src = now - DIGEST_S, "(last 2 h, no digest.read, scripts/orchbot.py)"

        def short(k, v):
            v = str(v)
            return v[:10] if k == "sha" else v[:80]

        rows, band = [], None
        for ev in self.jsonl_tail(self.conv / "events.jsonl"):
            kind, t = str(ev.get("event")), parse_ts(ev.get("ts"))
            if kind == "pace":
                prev, band = band, ev.get("band")
                if prev == band:
                    continue
            if t is None or t <= since:
                continue
            if kind == "finish":
                rid = str(ev.get("id"))
                commit = None
                if REPORT_ID_RE.fullmatch(rid):
                    try:
                        with open(self.root / "build" / "reports" / f"{rid}.md", encoding="utf-8",
                                  errors="replace") as f:
                            m = COMMIT_RE.search(f.read(REPORT_MAX))
                        h = HEX_RE.search(m.group(1)) if m else None
                        commit = h.group(0)[:10] if h else None
                    except OSError:
                        pass
                rsrc = f"events.jsonl + build/reports/{rid}.md" if commit else "events.jsonl"
                rows.append((t, f"{ev.get('ts')} finish lane={short('lane', ev.get('lane'))} id={rid[:80]} "
                                f"status={short('status', ev.get('status'))} commit={commit or 'unknown'} ({rsrc})"))
            elif kind in DIGEST_EVENTS and (kind != "page" or ev.get("result") == "sent"):
                kv = " ".join(f"{k}={short(k, ev[k])}" for k in DIGEST_EVENTS[kind] if k in ev)
                rows.append((t, f"{ev.get('ts')} {kind}{' ' + kv if kv else ''} (events.jsonl)"))

        st = self.quiet(self.conv / "status.json") or {}
        att = [a for a in st.get("attention") or [] if isinstance(a, dict)] \
            if isinstance(st.get("attention"), list) else []
        needs = []
        for a in att:
            iid, reason, t = str(a.get("id")), str(a.get("reason") or ""), parse_ts(a.get("since"))
            if t is not None and t > since:
                rows.append((t, f"{a.get('since')} attention {iid[:80]}: {reason[:120]} (status.json)"))
            if NEEDS_RE.search(reason) or NEEDS_RE.search(iid):
                try:
                    m = (self.conv / "ack" / cv.ack_name(iid)).stat().st_mtime
                    acked = t is not None and t <= m <= now
                except (OSError, ValueError):
                    acked = False
                if not acked:
                    needs.append(iid[:80])

        for d in self.jsonl_tail(self.out / "decisions.jsonl") + self.new:
            t = parse_ts(d.get("ts"))
            if t is None or t <= since:
                continue
            if d.get("mode") == "live":
                rows.append((t, f"{d['ts']} bot live {d.get('did')} {d.get('target')}: "
                                f"{str(d.get('saw'))[:120]} (decisions.jsonl)"))
            elif d.get("would_do") not in (None, "none"):
                rows.append((t, f"{d['ts']} bot {d['would_do']} {d.get('target')}: {str(d.get('saw'))[:120]} "
                                f"[{d.get('rule')}] (decisions.jsonl)"))
        rows = [r for _, r in sorted(rows, key=lambda r: r[0])]

        cut = 0

        def render():
            out = [f"# JARVIS digest at {cv.iso(now)} (scripts/orchbot.py)", f"since {cv.iso(since)} {src}", ""]
            if cut:
                out.append(f"- ... {cut} older rows not shown (size cap, scripts/orchbot.py)")
            out += [f"- {r}" for r in rows] if rows or cut else \
                [f"- nothing changed since {cv.iso(since)} (scripts/orchbot.py)"]
            out += ["", f"needs orchestrator: {', '.join(needs) or 'none'} (status.json)"]
            return "\n".join(clean(x) for x in out) + "\n"

        text = render()
        while rows and len(text.encode("utf-8")) > DIGEST_MAX:
            rows.pop(0)
            cut += 1
            text = render()
        # ponytail: byte cut as the last resort, only reachable with a huge needs list
        return text if len(text.encode("utf-8")) <= DIGEST_MAX else \
            text.encode("utf-8")[:DIGEST_MAX - 1].decode("utf-8", "ignore") + "\n"

    # ---- agreement (BOTAGREE) ----------------------------------------------------------------
    def check_agreement(self):
        ag = self.st.get("agreement")
        bad = ["not an object"] if not isinstance(ag, dict) else \
            [k for k, ok in AGREE_SHAPES.items() if k in ag and not ok(ag[k])]
        if bad:
            # DECISION TAKEN: reset only the agreement block (alternative: reset the whole state)
            self.st["agreement"] = agree_blank()
            self.decide(self.now(), "shadow.json agreement malformed", "escalate_orchestrator", "FAILCLOSED",
                        "shadow.json agreement", f"bad: {', '.join(bad)}; agreement reset")
        elif ag.get("version") != AGREE_VERSION:
            # DECISION TAKEN: a block of another version restarts the measurement (alternative: migrate it)
            self.st["agreement"] = agree_blank()
            self.decide(self.now(), "shadow.json agreement version", "none", "VERSION", "shadow.json agreement",
                        f"version {ag.get('version')} -> {AGREE_VERSION}; agreement reset")
        else:
            self.st["agreement"] = dict(agree_blank(), **ag)

    def observe(self, known):
        """(actuals, known): what the orchestrator/conveyor did since the last pass, read from files only.
        DECISION TAKEN: the first pass (or one after a reset) only records a baseline (alternative:
        count every file already there as an actual)."""
        now, out = self.now(), []
        first, known = known is None, known or {"acks": {}, "approvals": [], "backlog": [], "pace": {},
                                                          "board": []}

        def act(at, action, key, side, what):
            if not first:
                out.append({"at": at, "action": action, "key": key, "side": side, "what": what[:200]})

        def files(d):  # (name, path, mtime); a file that vanishes mid-scan is skipped, not the whole dir
            found = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_file():
                                found.append((e.name, e.path, e.stat().st_mtime))
                        except FileNotFoundError:
                            pass
            except FileNotFoundError:
                pass
            return sorted(found)

        acks, new, minute, self.deferred = {}, [], {}, set()
        for name, _, mt in files(self.conv / "ack"):
            if self.st.get("live", {}).get("acks", {}).get(name) == mt:  # the bot's own live ack
                acks[name] = mt
                continue
            # BOTFIX3D: a sweep is counted over the wall-clock minute (every orchestrator ack file there),
            # and an ack of a minute still running waits for the next pass, so a split sweep is still seen
            minute[int(mt // 60)] = minute.get(int(mt // 60), 0) + 1
            if mt > known["acks"].get(name, -1):
                if int(mt // 60) >= int(now // 60) and not first:
                    if int(mt // 60) == int(now // 60):  # BOTFIX3E: its pending ack waits for it (a future mtime does not)
                        self.deferred.add("ack:" + name.lower())
                    if name in known["acks"]:
                        acks[name] = known["acks"][name]
                    continue
                new.append((name, mt))
            acks[name] = mt
        for name, mt in [] if first else new:
            # BOTFIX3C: only an ack of a routine item the bot saw counts; a young one pre-empts the bot
            a, it = {"at": mt, "action": "ack", "key": "ack:" + name.lower(), "side": "orchestrator",
                     "what": f"ack/{name}"}, self.st["items"].get(name.lower())
            if minute[int(mt // 60)] >= SWEEP_N or not it or not it.get("routine"):
                self.verdict("excluded", None, dict(a, what=a["what"] + " (sweep, system or non-routine)"))
            elif mt - it["since"] < ACK_AGE_S:
                self.verdict("agree", None, dict(a, what=a["what"] + " (pre-empted)"))
            else:
                out.append(a)
        approvals = set()
        for d in (self.root / "build" / "approvals", self.root / "build" / "approvals" / "done"):
            for raw, path, mt in files(d):
                name = STAMP_RE.sub("", raw)
                ident = f"{name}@{int(mt)}"  # a push renames it into done/, keeping its mtime
                if not cv.APPROVAL_RE.fullmatch(name) or ident in approvals:
                    continue
                approvals.add(ident)
                if ident in known["approvals"] or first:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text = f.read(512)
                except FileNotFoundError:  # moved into done/ mid-pass: seen there, or next pass
                    approvals.discard(ident)
                    continue
                key = agree_key("write_key1", name)
                # DECISION TAKEN: an O42 pre-approval is conveyor-side, outside key-1 graduation (alternative: skip it)
                if text.startswith("auto-approved by the conveyor") or PRE_RE.search(text):
                    act(mt, "write_key1", key, "conveyor", f"{raw}: {text}")
                elif SALIH_RE.search(text):
                    act(mt, "ask_salih_key1", key, "orchestrator", f"{raw}: {text}")
                    if key.split("@")[0] != "key1:":  # BOTFIX3D: Salih's decision is a real key-1 ask, closed
                        act(mt, "none", "ask:" + key.split("@")[0], "salih", f"{raw}: {text}")
                else:
                    act(mt, "write_key1", key, "orchestrator", f"{raw}: {text}")
        slices = self.backlog.get("slices")
        ids = sorted({str(s.get("id")) for s in slices if isinstance(s, dict) and s.get("id")}) \
            if isinstance(slices, list) else known["backlog"]
        for i in ids:  # BOTFIX3C: only a new id whose prefix is a lane that finished BLOCKED before it
            if i not in known["backlog"] and self.st["blocked"].get(i.split("-")[0].lower(), now + 1) <= now:
                act(now, "queue_fix_slice", agree_key("queue_fix_slice", i.split("-")[0]), "orchestrator",
                    f"backlog id {i}")
        p, old = self.read_json(self.conv / "pace.json", {}), known["pace"]
        pace = dict({k: p[k] for k in PACE_KEYS if num(p.get(k))}, paused=bool(p.get("pause") or p.get("paused"))) \
            if p else old
        cut = [k for k in PACE_KEYS if num(pace.get(k)) and num(old.get(k)) and pace[k] < old[k]]
        if pace.get("paused") and not old.get("paused"):
            cut.append("paused")
        if cut:  # ponytail: one hold per pass whatever changed; per-key holds if they are ever told apart
            act(now, "hold_dispatch", "dispatch", "orchestrator", f"pace.json {', '.join(cut)}")
        # BOTFIX3D: an ask on the board (story.json "asks") is the real ask; its text names the bot's subject
        story = self.read_json(self.root / "build" / "pano" / "story.json", {})
        board = {}
        for b in story.get("asks") if isinstance(story.get("asks"), list) else []:
            if isinstance(b, dict) and str(b.get("title") or "").strip():
                text = " ".join(str(b.get(k) or "") for k in ("title", "blocks", "why", "cmds"))
                text = " ".join(text.lower().split())[:1000]
                # BOTFIX3E: keyed by title AND body: a new body under the same title is a new ask
                board[str(b["title"]).strip().lower()[:200] + "\x1f" + text] = text
        if not isinstance(story.get("asks"), list):
            board = dict.fromkeys(known.get("board", []), "")
        seen = set(known.get("board", []))
        for t in sorted(board.keys() - seen):
            title = t.split("\x1f")[0]
            if title not in seen:  # a title-only key is a pre-BOTFIX3E state: not new
                act(now, "none", "ask:board:" + board[t], "orchestrator", f"board ask {title}")
        return out, {"acks": acks, "approvals": sorted(approvals), "backlog": ids, "pace": pace,
                     "board": sorted(board)}

    def agree(self):
        """Match each pending decision to an actual action on the same key within MATCH_S. Every
        decided match is one verdict; a pass that decides nothing changes nothing (idempotent)."""
        now, ag = self.now(), self.st["agreement"]
        if ag["start"] is None:  # DECISION TAKEN: the first measuring pass (alternative: the first shadow pass)
            ag["start"] = now
        ag["since"] = cv.iso(ag["start"])[:10]
        actual, ag["known"] = self.observe(ag["known"])
        pend = sorted(ag["pending"], key=lambda e: e["at"])
        acts = sorted(ag["actual"] + actual + self.observed, key=lambda e: e["at"])
        used, keep = set(), []
        # BOTFIX3D: an ask already answered by one real actual (board or Salih) is never missed by the next one
        done = [d for d in ag.setdefault("answered", []) if now - d["at"] <= 2 * DAY_S]
        for a in acts:
            i = next((i for i, p in enumerate(pend) if i not in used and same_key(p["key"], a["key"])
                      and -(FIX_EARLY_S if p["key"].startswith("fix:") else MATCH_S) <= a["at"] - p["at"]
                      <= horizon(p["key"])), None)
            if i is not None:
                used.add(i)
                if pend[i]["key"].startswith("ask:"):
                    done.append({"at": pend[i]["at"], "key": pend[i]["key"], "action": "none"})
                self.verdict("agree" if pend[i]["action"] == a["action"] else "disagree", pend[i], a)
            elif now - a["at"] <= MATCH_S:
                keep.append(a)
            elif any(same_key(d["key"], a["key"]) and abs(a["at"] - d["at"]) <= DAY_S for d in done):
                pass
            elif a["side"] != "conveyor":  # DECISION TAKEN: a conveyor action is never missed; a Salih ask is
                self.verdict("missed", None, a)  # (alternative: count it missed too)
        ag["actual"], ag["answered"] = keep, done
        ag["pending"] = []
        for i, p in enumerate(pend):
            if i in used:
                continue
            if p["key"] in getattr(self, "deferred", ()):  # BOTFIX3E: never finalized while its ack waits for the next pass
                ag["pending"].append(p)
                continue
            gone = p["key"].startswith("ack:") and self.attn is not None and p["key"][4:] not in self.attn
            if now - p["at"] > horizon(p["key"]) or gone:  # BOTFIX3C: an ack whose item cleared is done
                # BOTFIX3D: the bot asked and nothing real was asked within the day -> disagree
                self.verdict("disagree" if p["key"].startswith("ask:") and not gone else "unmatched", p, None)
            else:
                ag["pending"].append(p)
        k1 = ag["key1"]
        ag["graduation"] = {"days": int((now - ag["start"]) // 86400), "pushes": k1["total"],
                            "agree_pct": round(100 * k1["agree"] / k1["total"], 1) if k1["total"] else 0.0}
        ag["graduation"]["ready"] = ag["graduation"]["days"] >= GRAD_DAYS and k1["total"] >= GRAD_PUSHES \
            and k1["agree"] == k1["total"]
        # BOTFIX3D: n and pct over evaluated verdicts (agree + disagree) only; the rest are side counters
        cls = {c: {"n": v.get("agree", 0) + v.get("disagree", 0),
                   "pct": round(100 * v.get("agree", 0) / (v.get("agree", 0) + v.get("disagree", 0)), 1)
                   if v.get("agree", 0) + v.get("disagree", 0) else 0.0,
                   **{k: v.get(k, 0) for k in ("bot_only", "missed", "excluded")}}
               for c, v in sorted(ag["classes"].items()) if sum(v.values())}
        ag["graduation"]["classes"] = cls
        ag["graduation"]["stage_a"] = ag["graduation"]["days"] >= GRAD_DAYS and all(
            cls.get(c, {}).get("n", 0) >= n and cls[c]["pct"] >= STAGE_PCT for c, n in STAGE_A.items())

    def verdict(self, kind, p, a):
        ag = self.st["agreement"]
        rec = {"ts": cv.iso(self.now()), "verdict": kind, "key": (p or a)["key"], "rule": p and p["rule"],
               "bot": p and {"at": cv.iso(p["at"]), "would_do": p["action"], "rule": p["rule"],
                             "target": p.get("target"), "detail": p.get("detail")},
               "orchestrator": a and {"at": cv.iso(a["at"]), "did": a["action"], "side": a["side"],
                                      "what": a.get("what")}}
        self.verdicts.append(rec)
        for book, name in ((ag["totals"], (p or a)["action"]), (ag["rules"], p and p["rule"])):
            if name:
                book.setdefault(name, {}).setdefault(kind, 0)
                book[name][kind] += 1
        c = klass(p, a)
        if c:  # BOTFIX3D: excluded is a per-class side counter, never in n or pct
            row = ag["classes"].setdefault(c, {})
            row["bot_only" if kind == "unmatched" else kind] = row.get("bot_only" if kind == "unmatched" else kind,
                                                                       0) + 1
        if c == "key1_orch" and kind != "excluded":  # BOTFIX3B: a conveyor auto-approval or O42 pre-approval is not a key-1 call
            ag["key1"]["total"] += 1
            ag["key1"]["agree"] += kind == "agree"
        if kind != "agree":
            ag["recent"] = (ag["recent"] + [rec])[-RECENT_N:]

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

    def changed(self, ln, rep, seen=frozenset()):
        """The range's files as far as the bot can tell without git: the lane's grant plus the
        report's FILES / ADJACENT FILES paths. DECISION TAKEN: no git diff (alternative: git diff
        origin/main..sha, a process the shadow bot must not need)."""
        files = [str(f) for f in ln.get("files") or []]
        # BOTREPLAY2: an integration report names its range as PICKS; each pick's own report holds
        # its FILES (and maybe its own PICKS). DECISION TAKEN: an empty block, a line naming no
        # lane with a report, a lane already on the path, or a pick adding no path empties the
        # whole list, so key 1 is asked (alternative: trust the other picks' lists).
        for header, block in PICKS_RE.findall(rep):
            block = header.partition(":")[2] + "\n" + block
            if not block.strip():
                return []
            for line in filter(str.strip, re.split(r"[;\n]", block)):
                subs = [t for t in PICK_TOK_RE.findall(line) if self.report(t, {})]  # every lane id
                if not subs or seen & set(subs):
                    return []
                for t in subs:
                    got = self.changed({}, self.report(t, {}), seen | {t})
                    if not got:
                        return []
                    files += got
        for m in FILES_RE.finditer(rep):
            for tok in re.split(r"[,\s]+", m.group(1)):
                tok = tok.lstrip("(`'\";:").rstrip(")`'\";:,.")  # never a leading '.': .claude, .env
                if "/" in tok or "\\" in tok or "." in tok:
                    files.append(tok)
        return files

    def approved(self, sha, done=False):
        names = []
        for d in ("approvals", "approvals/done")[:1 + done]:  # done: a pushed approval moved to done/
            try:
                names += os.listdir(self.root / "build" / d)
            except OSError:
                pass
        for n in names:
            m = cv.APPROVAL_RE.search(n)
            if m and sha.startswith(m.group(1)):
                return True
        return False

    # ---- event rules -------------------------------------------------------------------------
    def on_event(self, ev, at):
        handler = {"ci_result": self.ci_result, "finish": self.finish, "respawn": self.respawn,
                   "error": self.error, "push_blocked": self.push_blocked, "pace": self.pace,
                   "page": self.seen_page, "ci_rerun": self.seen_rerun,
                   "push": self.seen_push, "auto_approve": self.seen_auto,
                   "auto_approve_refused": self.seen_auto}.get(ev.get("event"))
        if handler:
            handler(ev, at, str(ev.get("lane") or ""))

    def seen_page(self, ev, at, lane):
        self.observed.append({"at": at, "action": "page_salih", "key": "salih", "side": "conveyor",
                              "what": f"page {ev.get('result')} lines={ev.get('lines')}"[:200]})

    def seen_rerun(self, ev, at, lane):
        self.observed.append({"at": at, "action": "rerun_ci_once", "key": agree_key("rerun_ci_once", lane),
                              "side": "conveyor", "what": f"ci_rerun {lane} {str(ev.get('sha'))[:12]} rc={ev.get('rc')}"})

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
        files, key = self.changed(ln, rep), agree_key("write_key1", f"push-{sha}-{lane}")
        if not files:  # BOTFIX3C: no file list - the conveyor's own auto_approve verdict decides, or wait
            self.st["waiting"][lane] = {"sha": sha, "at": at, "saw": saw, "key": key}
            return self.wait_key1(lane)
        why = protected(files)
        if why:
            self.decide(at, saw, "ask_salih_key1", "CONVSAFE", lane, why, key=key)
        else:
            self.decide(at, saw, "write_key1", "O26", lane, f"build/approvals/push-{sha}-{lane}", key=key)

    def seen_auto(self, ev, at, lane):
        ok = ev.get("event") == "auto_approve"
        self.st["auto"][lane] = {"ok": ok, "at": at, "sha": str(ev.get("approval") or "")[5:45] if ok else "",
                                 "why": "" if ok else str(ev.get("reason") or "")[:300]}
        self.wait_key1(lane)

    def wait_key1(self, lane):
        """Decide a waiting key 1 once the conveyor said auto_approve (O26 agree path) or
        auto_approve_refused (CONVSAFE, fail closed whatever the reason) for it; else keep waiting."""
        w, auto = self.st["waiting"].get(lane), self.st["auto"].get(lane)
        if not w or not auto or auto["at"] < w["at"] or (auto["ok"] and auto["sha"] != w["sha"]):
            return
        del self.st["waiting"][lane]
        if auto["ok"]:
            self.decide(auto["at"], w["saw"], "write_key1", "O26", lane,
                        f"build/approvals/push-{w['sha']}-{lane} (conveyor auto_approve)", key=w["key"])
        else:
            self.decide(auto["at"], w["saw"], "ask_salih_key1", "CONVSAFE", lane,
                        f"conveyor auto_approve_refused: {auto['why'] or 'no reason'}", key=w["key"])

    def finish(self, ev, at, lane):
        status = str(ev.get("status") or "").upper()
        ln = self.lanes.get(lane) or {}
        saw = f"finish {lane} {status}"
        if status == "GREEN":  # ci_done after CI_PENDING; deduped with its ci_result on the sha
            return self.key1(at, saw, lane, ln, str((ln.get("ci") or {}).get("sha") or ""))
        if status in cv.BAD:  # BOTFIX3C: a later backlog id with this prefix may be its fix slice
            self.st["blocked"][lane.split("-")[0].lower()] = at
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
        status = self.read_json(self.conv / "status.json", {})
        status = status if isinstance(status, dict) else {}
        listed = lambda k: [x for x in status.get(k) or [] if isinstance(x, dict)] \
            if isinstance(status.get(k) or [], list) else []  # noqa: E731  a bad status never aborts the acks
        wake = self.wakes.append
        if status.get("breaker") == "open":
            wake(("BREAKER", "push-gate", "escalate_orchestrator"))
        holds = self.st.setdefault("hold_since", {})
        if not isinstance(holds, dict):
            holds = self.st["hold_since"] = {}
        for target in ("disk", "memory"):
            if status.get(f"{target}_hold") is not True:
                holds.pop(target, None)
                continue
            since = holds.get(target)
            if not num(since) or not 0 <= now - since:
                since = holds[target] = now
            if now - since >= 30 * 60:
                wake(("HOLD", target, "hold_dispatch"))
        for ln in listed("lanes"):
            if ln.get("state") == "working" and not self.lane_finished(ln):
                at = max([t for t in (parse_ts(ln.get("started_at")), parse_ts(ln.get("updated_at"))) if t],
                         default=None)
                if at is not None and now - at > STUCK_S:
                    wake(("STUCK", str(ln.get("name") or ln.get("id")), "escalate_orchestrator"))
        if isinstance(status.get("attention"), list):
            self.attn = {}
        for it in listed("attention"):
            iid, reason, since = str(it.get("id")), str(it.get("reason") or ""), parse_ts(it.get("since"))
            ok = routine(iid, reason)
            name = cv.ack_name(iid).lower()
            self.attn[name] = since
            self.st["items"][name] = {"since": since if since is not None else now, "seen": now,
                                      "routine": ok and iid not in (cv.DRY_ID, cv.DISK_ID, cv.BREAKER_ID)}
            if iid == cv.DRY_ID:
                wake(("DRY", "backlog", "escalate_orchestrator"))
            elif iid == cv.DISK_ID:
                wake(("HOLD", "disk", "hold_dispatch"))
            elif iid != cv.BREAKER_ID and not ok and since is not None and now - since > ACK_AGE_S:
                wake(("ATTN", iid, "escalate_orchestrator"))
            if not ok or since is None or now - since <= ACK_AGE_S:
                continue
            ack = self.conv / "ack" / cv.ack_name(iid)
            if ack.exists() and since <= ack.stat().st_mtime <= now:  # BOTREPLAY2: a future ack hides nothing
                continue
            if iid.startswith(("cleanup:", "preserved:")) and not PROVEN_RE.match(reason):  # BOTFIX3D: M7
                # BOTV1: work not proven on main stays in front of a human (fail closed); logged once a day
                if self.once(f"keep:{iid}:{cv.iso(now)[:10]}", now):
                    self.decide(now, f"cleanup refusal {iid}: work not on main", "none", "KEEP-UNMERGED",
                                iid, reason[:300])
                # DECISION (Salih 2026-10-26): once KEEP-UNMERGED is logged the branch is preserved; ack it
            if self.once(f"ack:{iid}:{it.get('since')}", now):
                self.decide(now, f"routine attention {iid} {int((now - since) // 60)} min old", "ack", "O33",
                            iid, f"touch build/conveyor/ack/{cv.ack_name(iid)}")


def main(argv=None, now=time.time):
    ap = argparse.ArgumentParser(description="Orchestrator bot: shadow mode plus the live.json-gated live actions.")
    ap.add_argument("--shadow", action="store_true", required=True,
                    help="required; live actions only via build/bot/live.json (build/bot/OFF forces shadow)")
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
