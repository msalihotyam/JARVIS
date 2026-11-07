"""Pure project-run controls. Callers own persistence, clocks and side effects.

Pause uses the ledger's interrupted state and retains the exact phase to resume.
Wall time includes pauses; reaching a cap prevents starting more work. No API or
paid fallback is represented here. Large-run caps must come from the plan.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite


PHASES = frozenset({
    "clarifying", "planning", "awaiting_approval", "building", "accepting",
    "integrating", "needs_input",
})
STATES = PHASES | {"blocked:budget", "interrupted", "done", "failed", "cancelled"}
REVALIDATION_ORDER = ("identity", "capability", "target", "scope", "approval")
REVISION_CAP = 5
FAILED_SLICE_CAP = 3


def _count(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("counter must be a nonnegative integer")


def _seconds(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("time must be finite and nonnegative")
    if not isfinite(value) or value < 0:
        raise ValueError("time must be finite and nonnegative")


@dataclass(frozen=True)
class Budget:
    sessions: int
    wall_seconds: float
    model_calls: int

    def __post_init__(self) -> None:
        _count(self.sessions)
        _count(self.model_calls)
        _seconds(self.wall_seconds)


def budget_for(size: str, *, model_calls: int, large: Budget | None = None) -> Budget:
    """Model-call caps are plan supplied: D3 defines no default for them."""
    _count(model_calls)
    if size == "large" and large is not None:
        if large.model_calls != model_calls:
            raise ValueError("conflicting model-call caps")
        return large
    if large is not None:
        raise ValueError("explicit plan caps are only for large runs")
    if size == "small":
        return Budget(10, 2 * 3600, model_calls)
    if size == "medium":
        return Budget(30, 8 * 3600, model_calls)
    raise ValueError("unknown size or missing large plan budget")


@dataclass(frozen=True)
class Usage:
    sessions: int = 0
    model_calls: int = 0

    def __post_init__(self) -> None:
        _count(self.sessions)
        _count(self.model_calls)


def consume(usage: Usage, *, sessions: int = 0, model_calls: int = 0) -> Usage:
    """Record actual consumption, including overruns; never reset on resume."""
    _count(sessions)
    _count(model_calls)
    return Usage(usage.sessions + sessions, usage.model_calls + model_calls)


def budget_verdict(
    budget: Budget, usage: Usage, *, started_at: float, now: float,
    quota_available: bool = True,
) -> str:
    _seconds(started_at)
    _seconds(now)
    if now < started_at or type(quota_available) is not bool:
        raise ValueError("invalid clock or quota status")
    if (not quota_available or usage.sessions >= budget.sessions
            or usage.model_calls >= budget.model_calls
            or now - started_at >= budget.wall_seconds):
        return "blocked:budget"
    return "continue"


# N6 quota window (N6-04, Salih's rule O65 2026-11-04). The weekly quota resets Thursday
# 22:00 Istanbul; the whole week is free except the keep share Salih names for the
# current window (mirrors the conveyor's CONVRESERVE: stop at 100 - keep_pct).
# ponytail: fixed +03:00 (Istanbul has had no DST since 2016); swap for zoneinfo if that changes.
ISTANBUL = timezone(timedelta(hours=3), "Europe/Istanbul")
RESET_WEEKDAY = 3  # Thursday
RESET_HOUR = 22
KEEP_MAX = 30  # same ceiling as the conveyor's salih_keep_pct


def quota_window_end(window_start: str) -> float:
    """Epoch of the first Thursday 22:00 Istanbul strictly after ``window_start``.

    ``window_start`` is an ISO string as in pace.json; a naive one is Istanbul time.
    """
    try:
        start = datetime.fromisoformat(window_start)
    except (TypeError, ValueError):
        raise ValueError("window_start must be an ISO datetime string") from None
    if start.tzinfo is None:
        start = start.replace(tzinfo=ISTANBUL)
    start = start.astimezone(ISTANBUL)
    end = start.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0) + timedelta(
        days=(RESET_WEEKDAY - start.weekday()) % 7)
    if end <= start:
        end += timedelta(days=7)
    return end.timestamp()


def quota_verdict(
    *, now: float, usage_pct: object, exhausted: object, keep_pct: int | None,
    keep_window: str | None, window_start: str,
) -> str:
    """"continue" or "pause" for N6 work; pure, the caller supplies the clock and readings.

    Continue only when the budget said an explicit ``False`` for exhausted, the usage is
    a known number and it is below ``100 - keep``. Unknown usage pauses. The keep counts
    only while ``keep_window`` equals ``window_start`` and the window has not reset yet.
    """
    _seconds(now)
    if keep_pct is not None and (type(keep_pct) is not int or not 0 <= keep_pct <= KEEP_MAX):
        raise ValueError(f"keep_pct must be an int 0..{KEEP_MAX} or None")
    end = quota_window_end(window_start)
    if exhausted is not False:
        return "pause"
    if (isinstance(usage_pct, bool) or not isinstance(usage_pct, (int, float))
            or not isfinite(usage_pct) or usage_pct < 0):
        return "pause"  # unknown usage
    live = keep_pct is not None and keep_window == window_start and now < end
    return "pause" if usage_pct >= 100 - (keep_pct if live else 0) else "continue"


def revision_verdict(*, attempts: int, failed_slices: int) -> str:
    """Attempts counts revisions already tried; failed slices counts distinct slices.

    The driver records one failed slice after its cap, not one per attempt.
    """
    _count(attempts)
    _count(failed_slices)
    if failed_slices >= FAILED_SLICE_CAP:
        return "pause"
    if attempts >= REVISION_CAP:
        return "slice_failed_after_cap"
    return "continue"


@dataclass(frozen=True)
class RunControl:
    state: str
    resume_state: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError("unknown run state")
        if self.resume_state is not None and (
            self.resume_state not in PHASES
            or self.state not in {"interrupted", "blocked:budget"}
        ):
            raise ValueError("invalid saved phase")


def pause(run: RunControl) -> RunControl:
    if run.state in {"interrupted", "blocked:budget"}:
        return run
    if run.state not in PHASES:
        raise ValueError("cannot pause a terminal run")
    return RunControl("interrupted", run.state)


def cancel(run: RunControl) -> RunControl:
    if run.state == "cancelled":
        return run
    if run.state in {"done", "failed"}:
        raise ValueError("cannot cancel a terminal run")
    return replace(run, state="cancelled", resume_state=None)


def resume(
    run: RunControl, *, explicit: bool, revalidated: tuple[str, ...],
    budget_status: str,
) -> RunControl:
    """Consume Core's ordered successful checks; this does not perform them.

    Missing, failed, reordered or stale evidence must not be supplied as success.
    Budget extensions must already have been validated against the plan envelope.
    """
    if run.state not in {"interrupted", "blocked:budget"} or run.resume_state is None:
        raise ValueError("run has no resumable phase")
    if explicit is not True or revalidated != REVALIDATION_ORDER:
        raise ValueError("explicit resume and ordered revalidation required")
    if budget_status == "blocked:budget":
        return RunControl("blocked:budget", run.resume_state)
    if budget_status != "continue":
        raise ValueError("unknown budget verdict")
    return RunControl(run.resume_state)


def watchdog_verdict(*, last_progress_at: float, now: float, threshold: float) -> str:
    """A stall strictly past the threshold times out; no process is killed here.

    A timeout alone is not proof of death and never authorizes lease reaping.
    """
    for value in (last_progress_at, now, threshold):
        _seconds(value)
    if threshold == 0 or now < last_progress_at:
        raise ValueError("invalid watchdog clock or threshold")
    return "timeout" if now - last_progress_at > threshold else "continue"
