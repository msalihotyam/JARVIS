"""Deterministic supervision policy for project runs (PM35, D11/O50).

Pure: callers inject every fact (fix counts, quota readings, hour, free memory,
alarm history) and execute the returned Decision. No clock, process, network or
DB here. The rules port the orchestrator's playbook (ORCH_RULES O5/O11/O13/O15/
O23/O33, PM_DECISIONS D3/D8). A model step may only PROPOSE one of
ALLOWED_PROPOSALS; anything else fails closed to a needs_input question with
options and a default. Every Decision starts with (action, rule_id, reason).
Out-of-bounds caller parameters and a malformed situation raise ValueError.
"""

from math import isfinite
from typing import Mapping, NamedTuple


ALLOWED_PROPOSALS = frozenset({"pause", "ask_salih", "requeue", "split", "wait"})
CORE_ACTIONS = ALLOWED_PROPOSALS | {
    "none", "dispatch", "batch", "alarm", "suppress", "non_claude_only", "needs_input",
}
FIX_CAP = 2                 # O5/O15: no third fix on one target
QUOTA_LIMIT_PCT = 90.0      # O23: weekly hard stop (default; caller passes limit_pct)
AHEAD_PP = 3.0              # default: quota points over the pacing line that count as ahead
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
    if type(cap) is not int or not 0 <= cap <= FIX_CAP:
        raise ValueError(f"cap must be an integer 0..{FIX_CAP}")
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
    """O11/O13: split out blocked picks, manual-key picks in their own INT LAST,
    batch small. A pick without `manual: False` counts as manual."""
    names = [p["name"] for p in picks]
    if len(set(names)) != len(names) or not all(type(n) is str and n for n in names):
        raise ValueError("pick names must be unique nonempty strings")
    if type(max_batch) is not int or max_batch < 1:
        raise ValueError("max_batch must be a positive integer")
    if type(min_batch) is not int or min_batch < 1:
        raise ValueError("min_batch must be a positive integer")
    if int_running is not False:
        return Decision("wait", "O13.running", "an integration is running; do not stack")
    passed = [type(p.get("status")) is str and p.get("status") == "pass" for p in picks]
    ready = [p for p, ok in zip(picks, passed) if ok]
    held = [p["name"] for p, ok in zip(picks, passed) if not ok]
    if not ready:
        return Decision("wait", "O11.none_ready", "no pick has passed")
    groups = []
    for manual in (False, True):
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
                 limit_pct: float = QUOTA_LIMIT_PCT, ahead_pp: float = AHEAD_PP) -> Decision:
    """O23/D3: hard stop at the limit; only non-Claude workers when ahead or at night.

    used_pct may be one reading or several; the highest counts (fail closed).
    rate_pct_h/target_pct_h are optional as a pair: both None skips the rate rule;
    anything else must be a rate >= 0 and a target in (0, 100], or the run pauses.
    line_pct None means no pacing line; a supplied line outside 0..100 pauses."""
    readings = used_pct if isinstance(used_pct, (tuple, list)) else (used_pct,)
    values = [_num(v) for v in readings]
    limit, ahead = _num(limit_pct), _num(ahead_pp)
    if limit is not None and limit > 100:
        raise ValueError("limit_pct must be <= 100")
    if ahead is None or ahead < 0:
        raise ValueError("ahead_pp must be a finite number >= 0")
    if not values or None in values or min(values) < 0 or limit is None or limit <= 0:
        return Decision("pause", "O23.unknown", "quota reading or limit missing or invalid")
    used = max(values)
    if used >= limit:
        return Decision("pause", "O23.limit", f"quota {used:g}% >= {limit:g}%")
    if type(hour) is not int or not 0 <= hour <= 23:
        return Decision("pause", "O23.unknown", "hour missing or invalid")
    rate, target = _num(rate_pct_h), _num(target_pct_h)
    if (rate_pct_h is not None or target_pct_h is not None) and (
            rate is None or target is None or rate < 0 or not 0 < target <= 100):
        return Decision("pause", "O23.unknown", "burn rate or target invalid")
    line = _num(line_pct)
    if line_pct is not None and (line is None or not 0 <= line <= 100):
        return Decision("pause", "O23.unknown", "pacing line invalid")
    if line is None:
        return Decision("non_claude_only", "pace.unknown_line", "no pacing line")
    if used - line > ahead:
        return Decision("non_claude_only", "pace.ahead", f"quota {used:g}% vs line {line:g}%")
    if rate is not None and rate > target * RATE_SLACK:
        return Decision("non_claude_only", "pace.rate", f"{rate:g}%/h vs target {target:g}%/h")
    if NIGHT[0] <= hour < NIGHT[1]:
        return Decision("non_claude_only", "pace.night", f"night hour {hour}")
    return Decision("dispatch", "pace.on_line", f"quota {used:g}% vs line {line:g}%")


def memory_hold(free_gb, held: bool, ok_for_s=0, low_gb: float = MEM_LOW_GB,
                margin_gb: float = MEM_MARGIN_GB) -> Decision:
    """No new worker below low_gb; a hold releases at low+margin held MEM_OK_S.

    The caller's next `held` is `decision.action == "wait"`."""
    free, low_gb, margin_gb = _num(free_gb), _num(low_gb), _num(margin_gb)
    if low_gb is not None and low_gb <= 0:
        raise ValueError("low_gb must be > 0")
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
    # type() is str, not isinstance: a str subclass can override __eq__/__hash__.
    if type(proposal) is str:
        proposal = {"action": proposal}
    action = proposal.get("action") if isinstance(proposal, Mapping) else None
    reason = proposal.get("reason", "") if isinstance(proposal, Mapping) else ""
    reason = reason[:_REASON_MAX] if type(reason) is str else ""
    if type(action) is str and action in ALLOWED_PROPOSALS:
        return Decision(action, "D8.proposal", reason or "model proposal")
    shown = repr(action[:40]) if type(action) is str else type(action).__name__
    return _needs_input("D8.refused", f"refused model proposal {shown}")


def _alarm(item, cause, finished=(), alarmed=()) -> Decision:
    return filter_alarms([(item, cause)], finished, alarmed)[0]


_RULES = {"quota": quota_pacing, "memory": memory_hold, "alarm": _alarm,
          "fix": fix_chain, "integration": plan_integration}
_KEYS = frozenset(_RULES) | {"operator_pause", "proposal"}

# The ONE precedence table: (situation key, action; None = any action). supervise
# evaluates every present key, then returns the first row that matches. Memory is
# always checked; quota non_claude_only never hides alarm/fix/integration/proposal.
PRECEDENCE = (
    ("operator_pause", "pause"),
    ("quota", "pause"),
    ("memory", "wait"),
    ("alarm", "alarm"),
    ("fix", None),
    ("integration", None),
    ("proposal", None),
    ("quota", "non_claude_only"),
    ("memory", "dispatch"),
    ("quota", "dispatch"),
    ("alarm", "suppress"),      # a suppressed alarm hides nothing
)


def _evaluate(situation: Mapping) -> dict:
    found = {}
    if situation.get("operator_pause"):
        found["operator_pause"] = Decision("pause", "operator.pause", "Salih asked for a pause")
    for key, rule in _RULES.items():
        if key in situation:
            if not isinstance(situation[key], Mapping):
                raise ValueError(f"situation[{key!r}] must be a mapping")
            found[key] = rule(**situation[key])
    if "proposal" in situation:
        found["proposal"] = gate_proposal(situation["proposal"])
    return found


def supervise(situation: Mapping) -> Decision:
    """One decision by PRECEDENCE; no row matches -> none/idle.

    Unknown top-level keys -> needs_input. A non-Mapping situation or sub-situation,
    missing/extra/non-str sub-keys, out-of-bounds values or any error raised while
    evaluating raise ValueError (never another type); the caller must not dispatch
    on an exception."""
    if not isinstance(situation, Mapping):
        raise ValueError("situation must be a mapping")
    try:
        unknown = set(situation) - _KEYS
        if unknown:
            keys = ", ".join(sorted(map(repr, unknown)))
            return _needs_input("D8.unknown", f"unknown situation keys: {keys}"[:_REASON_MAX])
        found = _evaluate(situation)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"malformed situation: {type(exc).__name__}") from None
    for key, action in PRECEDENCE:
        d = found.get(key)
        if d is not None and action in (None, d.action):
            return d
    return Decision("none", "idle", "nothing to decide")
