"""APILANE1: the pure decision for the API overflow lane (unknown input = closed).

decide(state) -> Decision OPEN(model, effort, slice) | CLOSED(reason) | STOP(reason).
eligible(slice) -> True only for a tier T1 slice whose files touch no protected path and whose
class is not security. Pure: no I/O, no clock (today and the reading age are injected), no
network, no subprocess, no credential access. Models are an allowlist by construction:
only MODEL_SONNET, and MODEL_OPUS under the December 2026 month-end rule.
"""

import math
import posixpath
import re
import unicodedata
from datetime import date
from typing import NamedTuple

MODEL_SONNET, MODEL_OPUS, EFFORT = "sonnet", "opus", "high"
PER_SLICE_CAP_MAX_USD = 20.0  # ON default until the 2026-12-03 review
MAX_OVERFLOW_LANES = 1  # ON default until the 2026-12-03 review
FRESH_S = 900  # a credit reading older than 15 minutes is stale
MONTH_END_DAYS = 5
MONTH_END_LAST_DAY = date(2026, 12, 27)  # after this the month-end rule is inert
KEYS = frozenset({"subscription_blocked", "ready_slices", "envelope_remaining_usd", "per_slice_cap_usd",
                  "lanes_open", "running_cost_usd", "credits", "reading_age_s", "today"})
CREDIT_KEYS = frozenset({"remaining_usd", "expires"})


class Decision(NamedTuple):
    kind: str  # "OPEN" | "CLOSED" | "STOP"
    reason: str = ""
    model: str = ""
    effort: str = ""
    slice_id: str = ""


def closed(reason):
    return Decision("CLOSED", reason)


def stop(reason):
    return Decision("STOP", reason)


# --- protected set: mirror of scripts/conveyor.py (importing it would load subprocess/socket);
# tests/test_api_lane_policy.py proves the mirror agrees with the conveyor on a path corpus.
PROTECTED_DIRS = (".github/workflows/", ".claude/")
PROTECTED_FILES = {"scripts/test_parallel.py", "scripts/ci.ps1", "scripts/quality.ps1", "scripts/conveyor.py"}
ALIAS_DIRS = (".claude", ".github", "workflows", "data", ".jarvis")
WIN_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$", *(f"{d}{i}" for d in ("com", "lpt") for i in range(1, 10))}
CODEX_VETO = ("scripts/", ".claude/", ".github/", "data/")
# the conveyor's R1 security list (GRANT_DENY), lowercase substrings
GRANT_DENY = ("policy", "approv", "authori", "auth", "credential", "secret", "redact", "token",
              "password", "api_key", "apikey", "memory.py", "schema", "trusted_exec", "conveyor",
              "push", "release", "uv.lock", ".env", "scripts/", ".github/", ".claude/", "build/",
              "data/")
SKIPLIST_RE = re.compile(r"(skip|allow)[_-]?list|load[_-]?sensitive", re.I)
GRANT_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")  # conveyor grant gate: a file, never a folder
ID_RE = re.compile(r"[A-Za-z0-9_-]+")  # conveyor slice id


def _hidden_char(ch):
    o = ord(ch)
    return (unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") or o in (0x34F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0)
            or 0x180B <= o <= 0x180F or 0xFE00 <= o <= 0xFE0F or 0xE0000 <= o <= 0xE0FFF)


def _alias_suspect(p):
    if re.match(r"[\\/]{2}", p):
        return "UNC or device path"
    for c in re.split(r"[\\/]", p):
        n = unicodedata.normalize("NFKC", c).casefold()
        if re.search(r"~\d", c):
            return f"8.3 short name {c!r}"
        if ":" in c:
            return f"stream or drive {c!r}"
        if c.endswith(".") or c[-1:].isspace():
            return f"Win32-trimmed name {c!r}"
        if any(_hidden_char(ch) for ch in c):
            return f"hidden character in {c!r}"
        if n.split(".")[0].rstrip(" ") in WIN_DEVICES:
            return f"reserved device name {c!r}"
        if not c.isascii() and any(n == d or (len(n) == len(d) and all(not a.isascii() or a == b for a, b in zip(n, d)))
                                   for d in ALIAS_DIRS):
            return f"look-alike of a protected dir {c!r}"
    return None


def protected_path(p):
    if _alias_suspect(p):
        return True
    p = p.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (base == "uv.lock" or base.startswith(".env") or p.startswith(("data/", "dev/dashboard/"))
            or p.startswith(PROTECTED_DIRS) or "/.jarvis/" in "/" + p
            or any(s.startswith("riot") for s in p.split("/"))
            or base.endswith((".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".db", ".sqlite", ".sqlite3")))


def codex_veto(p):
    q = posixpath.normpath(p.replace("\\", "/").strip()).lower() if isinstance(p, str) and p.strip() else ""
    base = q.rsplit("/", 1)[-1]
    return (q in ("", ".", "..") or any(c in q for c in "*?[~") or ":" in q or q.startswith(("/", "../"))
            or any(s.endswith((".", " ")) for s in q.split("/"))
            or (q + "/").startswith(CODEX_VETO) or base in ("claude.md", "agents.md") or protected_path(q))


def protected(p):
    """The API lane's protected set: the conveyor's codex veto (covers protected_path, PROTECTED_DIRS,
    PROTECTED_FILES via scripts/) plus its skip/allow/load-sensitive list names, and stricter than the
    conveyor where it normalizes silently: padded names, backslashes and a bare .jarvis segment, plus
    the R1 security list (GRANT_DENY) and any non-ASCII path (homoglyphs of R1 words).
    Non-str (exact type) = protected."""
    if type(p) is not str or p != p.strip() or "\\" in p or not p.isascii():
        return True
    if not GRANT_PATH_RE.fullmatch(p) or ".." in p.split("/") or "." not in p.rsplit("/", 1)[-1].strip("."):
        return True  # a folder or extensionless name would cover the R1 files in it (conveyor grant gate)
    return (codex_veto(p) or bool(SKIPLIST_RE.search(p)) or ".jarvis" in p.casefold().split("/")
            or any(d in p.lower() for d in GRANT_DENY))


def eligible(s):
    """True only for a dict slice with tier exactly T1, a non-empty list of str files (conveyor key
    "files", plus "owned_files" if given) touching nothing protected, and class B or C (the classes
    Conveyor.route sends off-subscription; absent = unmarked = not eligible). Exact built-in types only."""
    if type(s) is not dict or type(s.get("tier")) is not str or s["tier"] != "T1":
        return False
    files = []
    for key in ("files", "owned_files"):
        v = s.get(key, [])
        if type(v) is not list:
            return False
        files += v
    cls = s.get("class")
    if type(cls) is not str or cls not in ("B", "C"):
        return False
    return bool(files) and not any(protected(f) for f in files)


EPOCH = date(2026, 11, 26)  # the rule was approved on this day; an earlier "today" means the clock went back


def _num(x):
    """A finite non-negative number as float, else None (bool, subclass, str, None, NaN, inf, -0.0, huge int)."""
    if type(x) not in (int, float):
        return None
    try:
        f = float(x)
    except OverflowError:
        return None
    return f if math.isfinite(f) and f >= 0 and math.copysign(1.0, f) > 0 else None


def _credits(v):
    if type(v) is not list:
        return None
    out = []
    for c in v:
        if type(c) is not dict or set(c) != CREDIT_KEYS or type(c["expires"]) is not date:
            return None
        r = _num(c["remaining_usd"])
        if r is None:
            return None
        out.append((r, c["expires"]))
    return out


def month_end(credits, today, margin):
    """December 2026 only: a lot expiring within MONTH_END_DAYS with more than the margin left.
    Inert after MONTH_END_LAST_DAY."""
    return today <= MONTH_END_LAST_DAY and any(
        exp.year == 2026 and exp.month == 12 and 0 <= (exp - today).days <= MONTH_END_DAYS and left > margin
        for left, exp in credits)


HOLD = "overflow lane already open"


def decide(state):
    """Unknown input = CLOSED while no overflow lane runs; once lanes_open is anything but exact int 0
    (a lane runs, or the count is unreadable) every unknown/clock-backwards/error result is STOP."""
    try:
        d = _decide(state)
    except Exception:  # never raise into the caller
        d = closed("internal error")
    try:
        lanes = state.get("lanes_open") if type(state) is dict else None
    except Exception:  # a hostile key can raise on lookup: unreadable count
        lanes = None
    if d.kind == "CLOSED" and d.reason != HOLD and not (type(lanes) is int and lanes == 0):
        return stop(d.reason)
    return d


def _decide(st):
    if type(st) is not dict or set(st) != KEYS:
        return closed("unknown state shape")
    quota, today = st["subscription_blocked"], st["today"]
    if type(quota) is not str or quota not in ("yes", "no", "unknown"):
        return closed("quota line unreadable")
    if type(today) is not date:
        return closed("today unreadable")
    if today < EPOCH:
        return closed("clock went backwards")
    age = st["reading_age_s"]
    if type(age) in (int, float) and age < 0:
        return closed("clock went backwards")
    age, env, cap, cost = (_num(st[k]) for k in ("reading_age_s", "envelope_remaining_usd", "per_slice_cap_usd",
                                                  "running_cost_usd"))
    lanes = st["lanes_open"]
    credits = _credits(st["credits"])
    slices = st["ready_slices"]
    if None in (age, env, cap, cost) or credits is None:
        return closed("number or credit unreadable")
    if type(lanes) is not int or _num(lanes) is None:
        return closed("lane count unreadable")
    if not 0 < cap <= PER_SLICE_CAP_MAX_USD:
        return closed("per-slice cap out of range")
    if type(slices) is not list:
        return closed("ready slices unreadable")
    stale = age > FRESH_S
    usable = sum(left for left, exp in credits if exp >= today)
    if cost >= cap:  # spend at the cap never (re)opens, whatever the lane count says
        return stop("per-slice cap reached")
    if lanes > 0:  # an overflow lane is running: only STOP or hold
        if lanes > MAX_OVERFLOW_LANES:
            return stop("too many overflow lanes")
        if env <= 0:
            return stop("envelope exhausted")
        if usable <= 0:
            return stop("credit exhausted")
        if stale:
            return stop("credit reading stale")
        return closed(HOLD)
    if stale:
        return closed("credit reading stale")
    if env < cap:
        return closed("envelope below per-slice cap")
    if usable < cap:
        return closed("credit below per-slice cap")
    ready = [s for s in slices if eligible(s) and type(s.get("id")) is str and ID_RE.fullmatch(s["id"])]
    if not ready:
        return closed("no eligible T1 slice")
    late = month_end(credits, today, cap)
    if quota == "unknown":
        return closed("quota line unknown")
    if quota == "no" and not late:
        return closed("subscription running")
    s = ready[0]
    model = MODEL_OPUS if late and type(s.get("model")) is str and s["model"] == MODEL_OPUS else MODEL_SONNET
    return Decision("OPEN", "", model, EFFORT, s["id"])
