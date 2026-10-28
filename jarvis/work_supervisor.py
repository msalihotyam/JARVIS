"""Deterministic supervision policy for project runs (PM35, D11/O50).

Pure: callers inject every fact (fix counts, quota readings, hour, free memory,
alarm history) and execute the returned Decision. No clock, process, network or
DB here. The rules port the orchestrator's playbook (ORCH_RULES O5/O11/O13/O15/
O23/O33, PM_DECISIONS D3/D8). A model step may only PROPOSE one of
ALLOWED_PROPOSALS; anything else fails closed to a needs_input question with
options and a default. Every Decision starts with (action, rule_id, reason).
"""

from math import isfinite
from typing import Mapping, NamedTuple


ALLOWED_PROPOSALS = frozenset({"pause", "ask_salih", "requeue", "split", "wait"})
CORE_ACTIONS = ALLOWED_PROPOSALS | {
    "none", "dispatch", "batch", "alarm", "suppress", "non_claude_only", "needs_input",
}
FIX_CAP = 2                 # O5/O15: no third fix on one target
QUOTA_LIMIT_PCT = 90.0      # O23: weekly hard stop
AHEAD_PP = 3.0              # quota points over the pacing line that count as ahead
RATE_SLACK = 1.2            # burn rate over target * this counts as ahead
NIGHT = (2, 8)              # local hours [start, end): only non-Claude workers
MEM_LOW_GB = 3.0            # conveyor min_free_mem_gb
MEM_MARGIN_GB = 1.5         # release at low + margin ...
MEM_OK_S = 300              # ... held continuously this long
INT_WAIT_S = 3600           # O11: a PASS waiting this long gets a small INT
INT_MIN_BATCH = 3
INT_MAX_BATCH = 6
_REASON_MAX = 200


class Decision(NamedTuple):
    action: str
    rule_id: str
    reason: str
    question: str = ""
    options: tuple = ()
    default: str = ""
    groups: tuple = ()


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if isfinite(value) else None


def _needs_input(rule_id: str, reason: str) -> Decision:
    return Decision(
        "needs_input", rule_id, reason,
        question="No rule covers this; which allowlisted action should the run take?",
        options=tuple(sorted(ALLOWED_PROPOSALS)), default="wait",
    )


def fix_chain(target: str, failed_fixes: int, cap: int = FIX_CAP) -> Decision:
    """O5/O15: after the cap-th failed fix ask one simplification question."""
    if type(failed_fixes) is not int or failed_fixes < 0:
        raise ValueError("failed_fixes must be a nonnegative integer")
    if failed_fixes < cap:
        return Decision("requeue", "O5.fix", f"{target}: fix {failed_fixes + 1}/{cap}")
    return Decision(
        "ask_salih", "O5.cap", f"{target}: {failed_fixes} fixes failed, no fix {cap + 1}",
        question=f"{target} failed {failed_fixes} fixes. Simplify the design or stop this target?",
        options=("simplify", "stop_target"), default="stop_target",
    )


def filter_alarms(alarms, finished=frozenset(), alarmed=frozenset()) -> list[Decision]:
    """O33: finished items never alarm; one alarm per cause, including past ones."""
    if isinstance(finished, str) or isinstance(alarmed, str):
        raise ValueError("finished/alarmed must be collections, not strings")
    seen = set(alarmed)
    out = []
    for item, cause in alarms:
        if item in finished:
            out.append(Decision("suppress", "O33.finished", f"{item} is finished"))
        elif cause in seen:
            out.append(Decision("suppress", "O33.duplicate", f"{cause} already alarmed"))
        else:
            seen.add(cause)
            out.append(Decision("alarm", "O33.new", f"{item}: {cause}"))
    return out


def plan_integration(picks, int_running: bool = False, oldest_wait_s: float = 0,
                     max_batch: int = INT_MAX_BATCH,
                     min_batch: int = INT_MIN_BATCH) -> Decision:
    """O11/O13: split out blocked picks, keep manual-key picks apart, batch small."""
    names = [p["name"] for p in picks]
    if len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
        raise ValueError("pick names must be unique nonempty strings")
    if type(max_batch) is not int or max_batch < 1:
        raise ValueError("max_batch must be a positive integer")
    if int_running is not False:
        return Decision("wait", "O13.running", "an integration is running; do not stack")
    ready = [p for p in picks if p.get("status") == "pass"]
    held = [p["name"] for p in picks if p.get("status") != "pass"]
    if not ready:
        return Decision("wait", "O11.none_ready", "no pick has passed")
    groups = []
    for manual in (True, False):
        part = [p["name"] for p in ready if (p.get("manual") is not False) is manual]
        groups += [tuple(part[i:i + max_batch]) for i in range(0, len(part), max_batch)]
    groups = tuple(groups)
    if held:
        return Decision("split", "O11.split", "held: " + ", ".join(held), groups=groups)
    wait = _num(oldest_wait_s)
    if wait is None or wait < 0:
        return Decision("wait", "O11.unknown_wait", "oldest wait missing or invalid")
    if len(ready) < min_batch and wait < INT_WAIT_S:
        return Decision("wait", "O11.small", f"{len(ready)} ready, oldest waited {wait:.0f}s")
    return Decision("batch", "O11.batch", f"{len(ready)} picks in {len(groups)} INT", groups=groups)


def quota_pacing(used_pct, line_pct, hour, rate_pct_h=None, target_pct_h=None,
                 limit_pct: float = QUOTA_LIMIT_PCT) -> Decision:
    """O23/D3: hard stop at the limit; only non-Claude workers when ahead or at night.

    used_pct may be one reading or several; the highest counts (fail closed)."""
    readings = used_pct if isinstance(used_pct, (tuple, list)) else (used_pct,)
    values = [_num(v) for v in readings]
    limit = _num(limit_pct)
    if not values or None in values or min(values) < 0 or limit is None or limit <= 0:
        return Decision("pause", "O23.unknown", "quota reading or limit missing or invalid")
    used = max(values)
    if used >= limit:
        return Decision("pause", "O23.limit", f"quota {used:g}% >= {limit:g}%")
    if type(hour) is not int or not 0 <= hour <= 23:
        return Decision("pause", "O23.unknown", "hour missing or invalid")
    line = _num(line_pct)
    if line is None:
        return Decision("non_claude_only", "pace.unknown_line", "no pacing line")
    if used - line > AHEAD_PP:
        return Decision("non_claude_only", "pace.ahead", f"quota {used:g}% vs line {line:g}%")
    rate, target = _num(rate_pct_h), _num(target_pct_h)
    if rate is not None and target is not None and rate > target * RATE_SLACK:
        return Decision("non_claude_only", "pace.rate", f"{rate:g}%/h vs target {target:g}%/h")
    if NIGHT[0] <= hour < NIGHT[1]:
        return Decision("non_claude_only", "pace.night", f"night hour {hour}")
    return Decision("dispatch", "pace.on_line", f"quota {used:g}% vs line {line:g}%")


def memory_hold(free_gb, held: bool, ok_for_s=0, low_gb: float = MEM_LOW_GB,
                margin_gb: float = MEM_MARGIN_GB) -> Decision:
    """No new worker below low_gb; a hold releases at low+margin held MEM_OK_S.

    The caller's next `held` is `decision.action == "wait"`."""
    free, low_gb, margin_gb = _num(free_gb), _num(low_gb), _num(margin_gb)
    if free is None or low_gb is None or margin_gb is None or margin_gb < 0:
        return Decision("wait", "mem.unknown", "free memory or threshold unknown")
    if free < low_gb:
        return Decision("wait", "mem.hold", f"free {free:g} GB < {low_gb:g} GB")
    ok = _num(ok_for_s)
    if held is not False and (free < low_gb + margin_gb or ok is None or ok < MEM_OK_S):
        return Decision("wait", "mem.hysteresis", f"free {free:g} GB, release at "
                        f"{low_gb + margin_gb:g} GB for {MEM_OK_S}s")
    return Decision("dispatch", "mem.ok", f"free {free:g} GB")


def gate_proposal(proposal) -> Decision:
    """D8: Core executes only allowlisted model proposals; the rest -> needs_input."""
    if isinstance(proposal, str):
        proposal = {"action": proposal}
    action = proposal.get("action") if isinstance(proposal, Mapping) else None
    reason = proposal.get("reason", "") if isinstance(proposal, Mapping) else ""
    reason = reason[:_REASON_MAX] if isinstance(reason, str) else ""
    if isinstance(action, str) and action in ALLOWED_PROPOSALS:
        return Decision(action, "D8.proposal", reason or "model proposal")
    return _needs_input("D8.refused", f"refused model proposal {str(action)[:40]!r}")


_KEYS = frozenset({"operator_pause", "quota", "memory", "alarm", "fix", "integration",
                   "proposal"})


def supervise(situation: Mapping) -> Decision:
    """One decision. Precedence: operator pause, quota, memory, alarm, fix chain,
    integration, model proposal. Quota/memory gates win unless they say dispatch."""
    unknown = set(situation) - _KEYS
    if unknown:
        return _needs_input("D8.unknown", "unknown situation keys: " + ", ".join(sorted(unknown)))
    if situation.get("operator_pause"):
        return Decision("pause", "operator.pause", "Salih asked for a pause")
    gate = None
    if "quota" in situation:
        gate = quota_pacing(**situation["quota"])
        if gate.action != "dispatch":
            return gate
    if "memory" in situation:
        gate = memory_hold(**situation["memory"])
        if gate.action != "dispatch":
            return gate
    if "alarm" in situation:
        a = situation["alarm"]
        return filter_alarms([(a["item"], a["cause"])], a.get("finished", ()),
                             a.get("alarmed", ()))[0]
    if "fix" in situation:
        return fix_chain(**situation["fix"])
    if "integration" in situation:
        return plan_integration(**situation["integration"])
    if "proposal" in situation:
        return gate_proposal(situation["proposal"])
    return gate or Decision("none", "idle", "nothing to decide")
