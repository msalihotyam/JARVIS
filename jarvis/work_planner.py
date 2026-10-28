"""PM11 planner call: ask the model for a plan, then check it deterministically.

The model only proposes JSON. Core parses it (Plan.from_json), validates it
(PM10), classifies it (PM08) from facts Core derives itself (never from a model
or caller flag, CLREV89 F1) and hashes it (PM09). Refusal reasons go back to the
model as data for at most MAX_REASKS re-asks. Nothing here executes a check,
starts a worker or writes into the project.
"""

import json
import os
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


def _inside(path: str, root: Path) -> bool:
    a, b = (os.path.normcase(os.path.realpath(p)) for p in (path, root))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def brief_facts(brief, *, large_forced: bool) -> tuple[str, dict[str, Any]]:
    """(project root, PM08 facts) derived from the confirmed brief and the disk.

    ``large_forced`` is clarify's manual-acceptance verdict; it maps onto the
    manual_checks fact. Unknown or unreadable facts fail toward the larger size."""
    root = brief.target_location.strip()
    try:
        existing = Path(root).exists()
        # at, under or above the checkout: a root above it still reaches it
        jarvis_repo = _inside(root, JARVIS_ROOT) or _inside(str(JARVIS_ROOT), Path(root))
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


def _check(text: str, root: str, facts: dict[str, Any]) -> Planned | list[Refusal]:
    if len(text.encode("utf-8", "replace")) > MAX_PLAN_BYTES:
        return [Refusal("oversized", "plan text over cap")]
    try:
        plan = Plan.from_json(text)
    except (ValueError, TypeError, RecursionError) as exc:
        return [Refusal("schema", str(exc)[:200])]
    refusals = validate(plan, root, jarvis_repo=facts["jarvis_repo"])
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
    """One ask plus up to MAX_REASKS re-asks; refusals are passed as data."""
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
        result = _check(text, root, facts)
        if isinstance(result, Planned):
            return Planned(result.plan, result.plan_hash, result.size, result.reasons, attempt)
        refusals = result
    return PlanFailed(tuple(refusals), MAX_REASKS + 1)
