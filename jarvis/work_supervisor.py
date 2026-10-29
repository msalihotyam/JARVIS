"""Deterministic supervision policy for project runs (PM35, D11/O50).

Pure: callers inject every fact (fix counts, quota readings, hour, free memory,
alarm history) and execute the returned Decision. No clock, process, network or
DB here. The rules port the orchestrator's playbook (ORCH_RULES O5/O11/O13/O15/
O23/O33, PM_DECISIONS D3/D8). A model step may only PROPOSE one of
ALLOWED_PROPOSALS; anything else fails closed to a needs_input question with
options and a default. Every Decision starts with (action, rule_id, reason) and
supervise() ends it with a worker constraint from CONSTRAINTS.

supervise() takes ONE complete, typed Situation; Situation.from_dict is the only
dict parser. A missing or invalid gate (operator_pause, policy, quota, memory)
pauses and never dispatches. Night/pacing thresholds are caller config (Policy).
Out-of-bounds rule parameters and malformed work items raise ValueError.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, NamedTuple


ALLOWED_PROPOSALS = frozenset({"pause", "ask_salih", "requeue", "split", "wait"})
CORE_ACTIONS = ALLOWED_PROPOSALS | {
    "none", "dispatch", "batch", "alarm", "suppress", "non_claude_only", "needs_input",
}
CONSTRAINTS = ("any", "non_claude_only", "none")   # loosest -> strictest worker set
FIX_CAP = 2                 # O5/O15: no third fix on one target
# DEFAULT_POLICY values (Salih's setting this week); callers pass their own Policy.
QUOTA_LIMIT_PCT = 100.0     # O23: weekly hard stop
CLAUDE_AT_NIGHT = True      # Claude workers allowed during NIGHT
AHEAD_PP = 3.0              # quota points over the pacing line that count as ahead
NIGHT = (2, 8)              # local hours [start, end)
MEM_LOW_GB = 3.0            # conveyor min_free_mem_gb
MEM_MARGIN_GB = 1.5         # release at low + margin ...
MEM_OK_S = 300              # ... held continuously this long
RATE_SLACK = 1.2            # burn rate over target * this counts as ahead
INT_WAIT_S = 3600           # O11: a PASS waiting this long gets a small INT
INT_MIN_BATCH = 3
INT_MAX_BATCH = 6
_REASON_MAX = 200
_ANY_WORKER = frozenset({"dispatch", "requeue", "split", "batch"})


class Decision(NamedTuple):
    action: str
    rule_id: str
    reason: str
    question: str = ""
    options: tuple = ()
    default: str = ""
    groups: tuple = ()
    constraint: str = "none"    # set by supervise(); "none" = start no worker


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if isfinite(value) else None


@dataclass(frozen=True)
class Policy:
    """Caller config (Salih's setting). Every field required; checked on build
    and again by every rule that reads it."""
    limit_pct: float
    ahead_pp: float
    night: tuple
    claude_at_night: bool
    mem_low_gb: float
    mem_margin_gb: float

    def __post_init__(self):
        self.check()

    def check(self):
        limit, ahead = _num(self.limit_pct), _num(self.ahead_pp)
        low, margin, night = _num(self.mem_low_gb), _num(self.mem_margin_gb), self.night
        if limit is None or not 0 < limit <= 100:
            raise ValueError("limit_pct must be a number in (0, 100]")
        if ahead is None or not 0 <= ahead <= 100:
            raise ValueError("ahead_pp must be a number in [0, 100]")
        # ponytail: one window, no wraparound (22, 6); add a second window if needed.
        if (type(night) is not tuple or len(night) != 2
                or any(type(h) is not int for h in night)
                or not 0 <= night[0] <= night[1] <= 24):
            raise ValueError("night must be (start, end) ints, 0 <= start <= end <= 24")
        if type(self.claude_at_night) is not bool:
            raise ValueError("claude_at_night must be a bool")
        if low is None or low <= 0:
            raise ValueError("mem_low_gb must be a finite number > 0")
        if margin is None or margin < 0:
            raise ValueError("mem_margin_gb must be a finite number >= 0")


DEFAULT_POLICY = Policy(QUOTA_LIMIT_PCT, AHEAD_PP, NIGHT, CLAUDE_AT_NIGHT,
                        MEM_LOW_GB, MEM_MARGIN_GB)


def _policy(policy) -> Policy:
    # type() is Policy, not isinstance: a subclass could override check().
    if type(policy) is not Policy:
        raise ValueError("policy must be a Policy")
    Policy.check(policy)    # a frozen dataclass can still be mutated via object.__setattr__
    return policy


def _needs_input(rule_id: str, reason: str) -> Decision:
    return Decision(
        "needs_input", rule_id, reason,
        question="No rule covers this; which allowlisted action should the run take?",
        options=tuple(sorted(ALLOWED_PROPOSALS)), default="wait",
    )


def fix_chain(target: str, failed_fixes: int, cap: int = FIX_CAP) -> Decision:
    """O5/O15: after the cap-th failed fix ask one simplification question."""
    if type(target) is not str or not target:
        raise ValueError("target must be a nonempty string")
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


def quota_pacing(policy: Policy, used_pct, line_pct, hour, rate_pct_h=None,
                 target_pct_h=None) -> Decision:
    """O23/D3: hard stop at policy.limit_pct; only non-Claude workers when ahead,
    and at policy.night unless policy.claude_at_night.

    used_pct may be one reading or several; the highest counts (fail closed).
    rate_pct_h/target_pct_h are optional as a pair: both None skips the rate rule;
    anything else must be a rate >= 0 and a target in (0, 100], or the run pauses.
    line_pct None means no pacing line; a supplied line outside 0..100 pauses."""
    policy = _policy(policy)
    readings = used_pct if isinstance(used_pct, (tuple, list)) else (used_pct,)
    values = [_num(v) for v in readings]
    if not values or None in values or min(values) < 0:
        return Decision("pause", "O23.unknown", "quota reading missing or invalid")
    used, limit = max(values), _num(policy.limit_pct)
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
    if used - line > _num(policy.ahead_pp):
        return Decision("non_claude_only", "pace.ahead", f"quota {used:g}% vs line {line:g}%")
    if rate is not None and rate > target * RATE_SLACK:
        return Decision("non_claude_only", "pace.rate", f"{rate:g}%/h vs target {target:g}%/h")
    start, end = policy.night
    if not policy.claude_at_night and start <= hour < end:
        return Decision("non_claude_only", "pace.night", f"night hour {hour}")
    return Decision("dispatch", "pace.on_line", f"quota {used:g}% vs line {line:g}%")


def memory_hold(policy: Policy, free_gb, held: bool, ok_for_s=0) -> Decision:
    """No new worker below policy.mem_low_gb; a hold releases at low+margin held
    MEM_OK_S. The caller's next `held` is `decision.action == "wait"`."""
    policy = _policy(policy)
    free, low = _num(free_gb), _num(policy.mem_low_gb)
    release = low + _num(policy.mem_margin_gb)
    if free is None:
        return Decision("wait", "mem.unknown", "free memory unknown")
    if free < low:
        return Decision("wait", "mem.hold", f"free {free:g} GB < {low:g} GB")
    ok = _num(ok_for_s)
    if held is not False and (free < release or ok is None or ok < MEM_OK_S):
        return Decision("wait", "mem.hysteresis", f"free {free:g} GB, release at "
                        f"{release:g} GB for {MEM_OK_S}s")
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


_GATES = ("operator_pause", "policy", "quota", "memory")
_WORK = {"alarm": _alarm, "fix": fix_chain, "integration": plan_integration,
         "proposal": gate_proposal}


@dataclass(frozen=True)
class Situation:
    """Everything supervise() reads. The gates are required: None (absent) pauses.
    A work item None is absent. `unknown` lists rejected dict keys (needs_input)."""
    operator_pause: object = None
    policy: object = None
    quota: object = None
    memory: object = None
    alarm: object = None
    fix: object = None
    integration: object = None
    proposal: object = None
    unknown: tuple = ()

    @classmethod
    def from_dict(cls, data) -> "Situation":
        """The ONE dict parser. An absent gate stays None (supervise pauses); a
        work key present as None, or any parse error, raises ValueError."""
        if not isinstance(data, Mapping):
            raise ValueError("situation must be a mapping")
        try:
            fields = _GATES + tuple(_WORK)
            unknown = tuple(sorted(repr(k) for k in data
                                   if type(k) is not str or k not in fields))
            kw = {k: data[k] for k in fields if k in data}
            for key in _WORK:
                if key in kw and kw[key] is None:
                    raise ValueError(f"situation[{key!r}] must not be None")
            return cls(**kw, unknown=unknown)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"malformed situation: {type(exc).__name__}") from None


# The ONE precedence table: (finding, action; None = any action), first match wins.
# The non_claude_only row returns nothing itself: it tightens the constraint of the
# decision a later row returns, so fix/alarm/integration/proposal can never lift it
# (H6). Memory ok is never a row; quota then decides.
PRECEDENCE = (
    ("operator_pause", "pause"),
    ("gate", None),                 # missing/invalid gate: pause or needs_input
    ("memory", "wait"),
    ("quota", "pause"),
    ("quota", "non_claude_only"),   # constraining row
    ("alarm", "alarm"),             # a suppressed alarm hides nothing
    ("fix", None),
    ("integration", None),
    ("proposal", None),
    ("quota", "dispatch"),
)


def _gates(s: Situation) -> dict:
    """Operator pause and the required gates; the first fault ends the scan."""
    pause = s.operator_pause
    if pause is True:
        return {"operator_pause": Decision("pause", "operator.pause", "Salih asked for a pause")}
    if pause is None:
        return {"gate": Decision("pause", "gate.missing", "operator_pause missing")}
    # type() is bool: never call __bool__ on an operator flag.
    if type(pause) is not bool:
        return {"gate": _needs_input(
            "operator.invalid", f"operator_pause must be a bool, not {type(pause).__name__}")}
    if s.policy is None:
        return {"gate": Decision("pause", "gate.missing", "policy missing")}
    try:
        policy = _policy(s.policy)
    except ValueError as exc:
        return {"gate": Decision("pause", "gate.invalid", f"policy invalid: {exc}")}
    found = {}
    for key, rule in (("memory", memory_hold), ("quota", quota_pacing)):
        sub = getattr(s, key)
        if sub is None:
            return {"gate": Decision("pause", "gate.missing", f"{key} missing")}
        if not isinstance(sub, Mapping):
            return {"gate": Decision("pause", "gate.invalid", f"{key} must be a mapping")}
        try:
            found[key] = rule(policy, **sub)
        except Exception as exc:
            return {"gate": Decision("pause", "gate.invalid",
                                     f"{key} invalid: {type(exc).__name__}")}
    return found


def _final(d: Decision, constraint: str) -> Decision:
    own = ("non_claude_only" if d.action == "non_claude_only"
           else "any" if d.action in _ANY_WORKER else "none")
    return d._replace(constraint=max(own, constraint, key=CONSTRAINTS.index))


def _decide(s: Situation) -> Decision:
    unknown = s.unknown
    if type(unknown) is not tuple or unknown:
        keys = ", ".join(map(str, unknown)) if type(unknown) is tuple else type(unknown).__name__
        return _needs_input("D8.unknown", f"unknown situation keys: {keys}"[:_REASON_MAX])
    found = _gates(s)
    if "operator_pause" not in found and "gate" not in found:
        for key, rule in _WORK.items():
            sub = getattr(s, key)
            if sub is None:
                continue
            if key == "proposal":
                found[key] = rule(sub)
            elif not isinstance(sub, Mapping):
                raise ValueError(f"situation.{key} must be a mapping")
            elif key == "integration" and ("int_running" not in sub or
                                           "min_batch" in sub or "max_batch" in sub):
                # Fail closed: the caller must say whether INT runs; batch sizes are
                # module constants, never per-call.
                raise ValueError("situation.integration needs int_running and no batch sizes")
            else:
                found[key] = rule(**sub)
    constraint = "any"
    for key, action in PRECEDENCE:
        d = found.get(key)
        if d is None or action not in (None, d.action):
            continue
        if action == "non_claude_only":
            constraint = "non_claude_only"
            continue
        return _final(d, constraint)
    return _final(found["quota"], constraint)   # non_claude_only and no work


def supervise(situation: Situation) -> Decision:
    """One decision by PRECEDENCE, with its worker constraint.

    A non-Situation (build one with Situation.from_dict) or unknown keys ->
    needs_input. A missing/invalid gate -> pause (an invalid operator_pause ->
    needs_input). Malformed work items or any error raised while evaluating them
    raise ValueError (never another type); the caller must not dispatch on it."""
    if type(situation) is not Situation:
        return _needs_input("D8.not_situation",
                            "supervise takes a Situation; build it with Situation.from_dict")
    try:
        return _decide(situation)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"malformed situation: {type(exc).__name__}") from None
