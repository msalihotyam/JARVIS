"""Pure Conveyor.route port; all quota/admission facts are supplied by the caller."""

import math
import posixpath


def _number(value, maximum=None):
    return (type(value) in (int, float) and 0 <= value
            and (maximum is None or value <= maximum)
            and (type(value) is int or math.isfinite(value)))


def _veto(path):
    """Conveyor codex_veto/protected_path rules, with no filesystem access."""
    q = posixpath.normpath(path.replace("\\", "/").strip()).lower()
    base = q.rsplit("/", 1)[-1]
    return (q in ("", ".", "..") or any(c in q for c in "*?[~")
            or ":" in q or q.startswith(("/", "../"))
            or any(s.endswith((".", " ")) for s in q.split("/"))
            or (q + "/").startswith(("scripts/", ".claude/", ".github/", "data/"))
            or base in ("claude.md", "agents.md", "uv.lock")
            or base.startswith(".env") or q.startswith("dev/dashboard/")
            or "/.jarvis/" in "/" + q
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
            or facts["worker"] not in ("auto", "claude", "codex", "jules")):
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
    for worker in ("codex", "jules"):
        if not _number(h.get(worker), 100 if worker == "codex" else None):
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
    candidate = "codex" if w == "auto" else w
    if candidate == "jules":
        if facts["new_project"]:
            return "claude", "new_project"
        if not facts["linux_ok"]:
            return "claude", "linux_required"
    if not admitted[candidate]:
        return "claude", "admission_refused"
    if h[candidate] < (10 if candidate == "codex" else 1):
        return "claude", "low_headroom"
    if w != "auto":
        return w, "explicit"
    return (("codex", "more_headroom") if 100 - facts["usage"] < h["codex"]
            else ("claude", "claude_headroom"))
