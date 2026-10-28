"""PM11 planner call: ask the model for a plan, then check it deterministically.

The model only proposes JSON. Core parses it (Plan.from_json), validates it
(PM10), classifies it (PM08) from facts Core derives itself (never from a model
or caller flag, CLREV89 F1) and hashes it (PM09). Refusal reasons go back to the
model as data for at most MAX_REASKS re-asks. Nothing here executes a check,
starts a worker or writes into the project.
"""

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from jarvis.work_controls import budget_for
from jarvis.work_plan import Plan, Refusal, classify, validate

#: The JARVIS checkout; a project root at or under it is the JARVIS repo (I12).
JARVIS_ROOT = Path(__file__).resolve().parents[1]
MAX_REASKS = 2
#: Model output above this many UTF-8 bytes is refused unparsed.
MAX_PLAN_BYTES = 64 * 1024


@dataclass(frozen=True)
class Planned:
    plan: Plan
    plan_hash: str
    size: str
    reasons: tuple[str, ...]
    attempts: int


@dataclass(frozen=True)
class PlanFailed:
    refusals: tuple[Refusal, ...]
    attempts: int


def _names(path: Path) -> list[str]:
    # Win32 drops trailing dots/spaces, so "JARVIS. " opens "JARVIS".
    return [p.rstrip(". ") or p for p in path.parts]


def _keys(names: list[str]) -> list[str]:
    return [unicodedata.normalize("NFKC", os.path.normcase(n)).casefold() for n in names]


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def checkout_relation(root: str) -> tuple[str | None, str]:
    """Where an absolute root sits relative to the JARVIS checkout (CDXINT109 F1).

    ("inside", prefix) when root is the checkout or under it, prefix being its
    checkout-relative "a/b/" form; ("ancestor", "") when it holds the checkout;
    (None, "") otherwise. Paths are resolved first (realpath: case, 8.3 names,
    symlinks, junctions), then compared by name and by file identity, so
    another spelling of the same tree still overlaps. Raises ValueError or
    OSError when the root cannot be judged; callers fail closed."""
    # drive paths only: a UNC or device root would reach the network on stat
    if not re.match(r"[A-Za-z]:[\\/]", root) or not os.path.isabs(root):
        raise ValueError("root is not an absolute drive path")
    real, home = (Path(os.path.realpath(p)) for p in (root, JARVIS_ROOT))
    names, marks = _names(real), _names(home)
    keys, mark_keys = _keys(names), _keys(marks)
    if keys[:len(marks)] == mark_keys:
        return "inside", "".join(n + "/" for n in names[len(marks):])
    if mark_keys[:len(keys)] == keys:
        return "ancestor", ""
    mark = _identity(home)
    for depth in range(len(real.parts), 0, -1) if mark else ():
        if _identity(Path(*real.parts[:depth])) == mark:
            return "inside", "".join(n + "/" for n in names[depth:])
    if _identity(real) in {_identity(p) for p in home.parents} - {None}:
        return "ancestor", ""
    return None, ""


def brief_facts(brief, *, large_forced: bool) -> tuple[str, dict[str, Any]]:
    """(project root, PM08 facts) derived from the confirmed brief and the disk.

    ``large_forced`` is clarify's manual-acceptance verdict; it maps onto the
    manual_checks fact. Unknown or unreadable facts fail toward the larger size."""
    root = brief.target_location.strip()
    try:
        # at, under or above the checkout: a root above it still reaches it;
        # judged first so a refused root is never stat'ed
        jarvis_repo = checkout_relation(root)[0] is not None
        existing = Path(root).exists()
    except (OSError, ValueError):
        existing = jarvis_repo = True
    risky = brief.outgoing_or_money is not False
    return root, {
        "target_folders": [root],
        "existing_repo": existing,
        "outgoing_actions": risky,
        "money_actions": risky,
        "manual_checks": large_forced or any(e.manual for e in brief.acceptance_examples),
        "jarvis_repo": jarvis_repo,
    }


def _check(text: str, root: str, facts: dict[str, Any],
           repo_prefix: str | None) -> Planned | list[Refusal]:
    if len(text.encode("utf-8", "replace")) > MAX_PLAN_BYTES:
        return [Refusal("oversized", "plan text over cap")]
    try:
        plan = Plan.from_json(text)
    except (ValueError, TypeError, RecursionError) as exc:
        return [Refusal("schema", str(exc)[:200])]
    refusals = validate(plan, root, jarvis_repo=facts["jarvis_repo"], repo_prefix=repo_prefix)
    size, reasons = classify(facts, plan)
    if size != "large":
        # D3 caps: a small/medium plan may not ask for more than its size grants.
        cap = budget_for(size, model_calls=plan.budgets.model_calls)
        if (plan.budgets.worker_sessions > cap.sessions
                or plan.budgets.wall_clock_seconds > cap.wall_seconds):
            refusals.append(Refusal("oversized", f"budgets exceed {size} caps"))
    if refusals:
        return refusals
    return Planned(plan, plan.plan_hash, size, tuple(reasons), 0)


def plan_project(
    brief, root: str, facts: dict[str, Any],
    plan_model: Callable[[dict[str, Any]], Any],
) -> Planned | PlanFailed:
    """One ask plus up to MAX_REASKS re-asks; refusals are passed as data.

    A root overlapping the JARVIS checkout has every target checked as the
    checkout sees it; a root above the checkout, or one that cannot be
    judged, is refused before any model call."""
    try:
        relation, prefix = checkout_relation(root)
    except (OSError, ValueError) as exc:
        return PlanFailed((Refusal("root_unresolvable", type(exc).__name__),), 0)
    if relation == "ancestor":
        return PlanFailed((Refusal("jarvis_ancestor", root),), 0)
    if relation and facts.get("jarvis_repo") is not True:
        return PlanFailed((Refusal("unknown_fact", "jarvis_repo"),), 0)
    repo_prefix = prefix if relation else None
    refusals: list[Refusal] = []
    for attempt in range(1, MAX_REASKS + 2):
        context = {
            "brief": asdict(brief), "project_root": root, "attempt": attempt,
            "refusals": [{"code": r.code, "detail": r.detail} for r in refusals],
        }
        try:
            raw = plan_model(json.loads(json.dumps(context)))
            text = raw if type(raw) is str else json.dumps(raw, allow_nan=False)
        except Exception as exc:  # adapter failures are refusals, never a crash
            refusals = [Refusal("model_error", type(exc).__name__)]
            continue
        result = _check(text, root, facts, repo_prefix)
        if isinstance(result, Planned):
            return Planned(result.plan, result.plan_hash, result.size, result.reasons, attempt)
        refusals = result
    return PlanFailed(tuple(refusals), MAX_REASKS + 1)
