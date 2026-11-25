"""Pure Conveyor.route port; all quota/admission facts are supplied by the caller."""

import math
import posixpath
import re
import unicodedata
from types import MappingProxyType
from typing import NamedTuple


class Headroom(NamedTuple):
    maximum: int | None  # valid headroom range is 0..maximum (None = unbounded)
    minimum: int         # below this the worker is low_headroom


class WorkerSpec(NamedTuple):
    explicit: bool               # facts["worker"] may name it
    auto: bool                   # the one worker "auto" weighs against Claude
    headroom: Headroom | None    # None = no headroom/admission facts (Claude)
    reviewer: str                # cross-provider reviewer (work_review.REVIEWER_FOR)


# One row per worker; read-only (proxy over records that cannot be reassigned).
WORKERS = MappingProxyType({
    "claude": WorkerSpec(explicit=True, auto=False, headroom=None, reviewer="codex"),
    "codex": WorkerSpec(explicit=True, auto=True, headroom=Headroom(100, 10),
                        reviewer="claude"),
    "jules": WorkerSpec(explicit=True, auto=False, headroom=Headroom(None, 1),
                        reviewer="claude"),
})
_EXPLICIT = tuple(n for n, w in WORKERS.items() if w.explicit)
_TRACKED = tuple((n, w.headroom) for n, w in WORKERS.items() if w.headroom)
(_AUTO,) = (n for n, w in WORKERS.items() if w.auto)  # exactly one, checked at import


# Twins of scripts/conveyor.py ALIAS_DIRS, WIN_DEVICES, _hidden_char, _alias_suspect (PROTALIAS1);
# tests.test_workers_route pins both sides and checks them over one alias corpus.
_ALIAS_DIRS = (".claude", ".github", "workflows", "data", ".jarvis")
_WIN_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$",
                *(f"{d}{i}" for d in ("com", "lpt") for i in range(1, 10))}


def _number(value, maximum=None):
    return (type(value) in (int, float) and 0 <= value
            and (maximum is None or value <= maximum)
            and (type(value) is int or math.isfinite(value)))


def _hidden_char(ch):
    """C0/C1 control, format (bidi, zero-width, BOM, tags) or another default-ignorable code point."""
    o = ord(ch)
    return (unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")
            or o in (0x34F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0)
            or 0x180B <= o <= 0x180F or 0xFE00 <= o <= 0xFE0F or 0xE0000 <= o <= 0xE0FFF)


def _alias_suspect(p):
    """True when p may name something other than it reads (8.3 alias, stream/drive,
    Win32-trimmed name, device/UNC prefix, hidden char, reserved device, look-alike dir)."""
    if re.match(r"[\\/]{2}", p):
        return True
    for c in re.split(r"[\\/]", p):
        n = unicodedata.normalize("NFKC", c).casefold()
        if (re.search(r"~\d", c) or ":" in c or c.endswith(".") or c[-1:].isspace()
                or any(_hidden_char(ch) for ch in c)
                or n.split(".")[0].rstrip(" ") in _WIN_DEVICES):
            return True
        # NFKC folds fullwidth; a same-length name equal at every ASCII position catches Cyrillic/Greek look-alikes
        if not c.isascii() and any(n == d or (len(n) == len(d) and all(not a.isascii() or a == b
                                                                       for a, b in zip(n, d)))
                                   for d in _ALIAS_DIRS):
            return True
    return False


def _veto(path):
    """Conveyor codex_veto/protected_path rules, with no filesystem access.

    jarvis/ never imports scripts/; tests.test_workers_route fails if this drifts.
    """
    q = posixpath.normpath(path.replace("\\", "/").strip()).lower()
    base = q.rsplit("/", 1)[-1]
    return (q in ("", ".", "..") or _alias_suspect(q) or any(c in q for c in "*?[~")
            or ":" in q or q.startswith(("/", "../"))
            or any(s.endswith((".", " ")) for s in q.split("/"))
            or (q + "/").startswith(("scripts/", ".claude/", ".github/", "data/"))
            or base in ("claude.md", "agents.md", "uv.lock")
            or base.startswith(".env") or q.startswith("dev/dashboard/")
            or "/.jarvis/" in "/" + q
            or any(s.startswith("riot") for s in q.split("/"))
            or base.endswith((".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
                              ".db", ".sqlite", ".sqlite3")))


def route(facts):
    """Return (worker, reason_key), defaulting to Claude on invalid facts.

    Required keys: tier, class, files, veto (vetoed file paths), linux_ok,
    new_project, worker (auto/claude/codex/jules), usage (Claude percentage),
    headroom ({codex: percentage, jules: available sessions}), subscription_only,
    and admitted ({codex: bool, jules: bool}). Admission flags represent fresh,
    subscription-only quota checks performed outside this pure module.
    Auto selects only Codex, as in Conveyor.route; Jules requires an explicit
    request. Stable reason keys are intended for decision tables and callers.
    """
    default = ("claude", "invalid_input")
    if type(facts) is not dict:
        return default
    required = {"tier", "class", "files", "veto", "linux_ok", "new_project",
                "worker", "usage", "headroom", "subscription_only", "admitted"}
    if not required <= facts.keys():
        return default
    for key in ("tier", "class", "worker"):
        if type(facts[key]) is not str:
            return default
    if (facts["tier"] not in ("T0", "T1", "T2")
            or facts["class"] not in ("A", "B", "C")
            or facts["worker"] not in ("auto", *_EXPLICIT)):
        return default
    for key in ("linux_ok", "new_project", "subscription_only"):
        if type(facts[key]) is not bool:
            return default
    for key in ("files", "veto"):
        if type(facts[key]) is not list or any(type(p) is not str for p in facts[key]):
            return default
    if not _number(facts["usage"], 100):
        return "claude", "usage_unknown"
    h, admitted = facts["headroom"], facts["admitted"]
    if type(h) is not dict or type(admitted) is not dict:
        return default
    for worker, limits in _TRACKED:
        if not _number(h.get(worker), limits.maximum):
            return "claude", "headroom_unknown"
        if type(admitted.get(worker)) is not bool:
            return default
    if not facts["subscription_only"]:
        return "claude", "subscription_required"
    w = facts["worker"]
    if w == "claude":
        return "claude", "explicit"
    if facts["tier"] not in ("T0", "T1"):
        return "claude", "tier"
    if facts["class"] not in ("B", "C"):
        return "claude", "class"
    if not facts["files"]:
        return "claude", "no_files"
    veto = {posixpath.normpath(p.replace("\\", "/")).lower() for p in facts["veto"]}
    if any(_veto(p) or posixpath.normpath(p.replace("\\", "/")).lower() in veto
           for p in facts["files"]):
        return "claude", "veto"
    candidate = _AUTO if w == "auto" else w
    if candidate == "jules":
        if facts["new_project"]:
            return "claude", "new_project"
        if not facts["linux_ok"]:
            return "claude", "linux_required"
    if not admitted[candidate]:
        return "claude", "admission_refused"
    if h[candidate] < WORKERS[candidate].headroom.minimum:
        return "claude", "low_headroom"
    if w != "auto":
        return w, "explicit"
    return ((_AUTO, "more_headroom") if 100 - facts["usage"] < h[_AUTO]
            else ("claude", "claude_headroom"))
