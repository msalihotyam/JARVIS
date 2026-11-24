"""Pure Conveyor.route port; all quota/admission facts are supplied by the caller."""

import math
import posixpath
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


def _number(value, maximum=None):
    return (type(value) in (int, float) and 0 <= value
            and (maximum is None or value <= maximum)
            and (type(value) is int or math.isfinite(value)))


def _veto(path):
    """Conveyor codex_veto/protected_path rules, with no filesystem access.

    jarvis/ never imports scripts/; tests.test_workers_route fails if this drifts.
    """
    q = posixpath.normpath(path.replace("\\", "/").strip()).lower()
    base = q.rsplit("/", 1)[-1]
    return (q in ("", ".", "..") or any(c in q for c in "*?[~")
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
