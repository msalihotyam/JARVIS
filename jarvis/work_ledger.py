"""Project-mode work ledger contracts: runs, steps, events and their states.

Pure data and rules only. Nothing here performs I/O, opens a database, reads
the wall clock, calls a model or decides authority; every timestamp is
injected by the caller and the store (PM02) persists what this module
validates. A legal transition here is necessary, never sufficient: approval
grants, budget checks, the scope gate and the acceptance verdict stay with
their own owners, and this module only refuses moves that no owner may make.

Decisions encoded here (PM_DECISIONS): D2 caps a step at
``MAX_REVISIONS`` revision attempts, after which only ``failed_after_cap`` is
legal, and a run pauses once ``FAILED_STEPS_PAUSE`` steps failed after the
cap; D8 makes ``needs_input`` block only the steps that depend on the open
question, so the run itself waits for input only when nothing else can move;
D9 lets a small/medium run go from planning straight to building on the
read-back "yes", while a large run must pass ``awaiting_approval``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

#: D2: revision attempts allowed per step after its first attempt.
MAX_REVISIONS = 5

#: D2: steps failed after the revision cap that pause the whole run.
FAILED_STEPS_PAUSE = 3

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ACTION = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")


class WorkLedgerError(ValueError):
    """A ledger value or move that the contract refuses."""


class IllegalTransition(WorkLedgerError):
    """A state change that is not in the transition table or fails a guard."""


class RunSize(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class RunState(str, Enum):
    CLARIFYING = "clarifying"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    BUILDING = "building"
    ACCEPTING = "accepting"
    INTEGRATING = "integrating"
    NEEDS_INPUT = "needs_input"
    BLOCKED_BUDGET = "blocked:budget"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    ACCEPTING = "accepting"
    ACCEPTED = "accepted"
    NEEDS_INPUT = "needs_input"
    BLOCKED_BY_GATE = "blocked_by_gate"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    FAILED_AFTER_CAP = "failed_after_cap"
    CANCELLED = "cancelled"


class EventKind(str, Enum):
    """Ledger event kinds; the notification points of the stage-2 plan."""

    RUN_STATE = "run_state"
    STEP_STATE = "step_state"
    MODEL_OUTPUT = "model_output"
    QUESTIONS_READY = "questions_ready"
    READBACK_READY = "readback_ready"
    PLAN_READY = "plan_ready"
    NEEDS_INPUT = "needs_input"
    BLOCKED_BUDGET = "blocked:budget"
    SLICE_FAILED_AFTER_CAP = "slice_failed_after_cap"
    APPROVAL_REQUIRED = "approval_required"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    PROGRESS = "progress"
    DONE = "done"
    INTERRUPTED = "interrupted"


#: Run phases that do work; a wait state remembers which one to resume.
ACTIVE_RUN_STATES = frozenset(
    {
        RunState.CLARIFYING,
        RunState.PLANNING,
        RunState.AWAITING_APPROVAL,
        RunState.BUILDING,
        RunState.ACCEPTING,
        RunState.INTEGRATING,
    }
)

#: Run states that hold the run until an answer, budget, resume or restart.
WAIT_RUN_STATES = frozenset(
    {
        RunState.NEEDS_INPUT,
        RunState.BLOCKED_BUDGET,
        RunState.PAUSED,
        RunState.INTERRUPTED,
    }
)

TERMINAL_RUN_STATES = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})

_ENDINGS = frozenset({RunState.PAUSED, RunState.INTERRUPTED, RunState.FAILED, RunState.CANCELLED})

#: Every legal run move. Leaving a wait state for an active one is further
#: restricted to the remembered ``resume_state``; planning's exit depends on
#: the run size (D9).
RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.CLARIFYING: _ENDINGS
    | {RunState.PLANNING, RunState.NEEDS_INPUT, RunState.BLOCKED_BUDGET},
    RunState.PLANNING: _ENDINGS
    | {
        RunState.AWAITING_APPROVAL,
        RunState.BUILDING,
        RunState.NEEDS_INPUT,
        RunState.BLOCKED_BUDGET,
    },
    RunState.AWAITING_APPROVAL: _ENDINGS | {RunState.BUILDING, RunState.PLANNING},
    RunState.BUILDING: _ENDINGS
    | {RunState.ACCEPTING, RunState.NEEDS_INPUT, RunState.BLOCKED_BUDGET},
    RunState.ACCEPTING: _ENDINGS
    | {
        RunState.INTEGRATING,
        RunState.BUILDING,
        RunState.NEEDS_INPUT,
        RunState.BLOCKED_BUDGET,
    },
    RunState.INTEGRATING: _ENDINGS
    | {RunState.DONE, RunState.BUILDING, RunState.BLOCKED_BUDGET},
    RunState.NEEDS_INPUT: ACTIVE_RUN_STATES
    | {RunState.PAUSED, RunState.INTERRUPTED, RunState.FAILED, RunState.CANCELLED},
    RunState.BLOCKED_BUDGET: ACTIVE_RUN_STATES
    | {RunState.PAUSED, RunState.INTERRUPTED, RunState.FAILED, RunState.CANCELLED},
    RunState.PAUSED: ACTIVE_RUN_STATES
    | {RunState.INTERRUPTED, RunState.FAILED, RunState.CANCELLED},
    RunState.INTERRUPTED: ACTIVE_RUN_STATES | {RunState.FAILED, RunState.CANCELLED},
    RunState.DONE: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

#: Step states from which a new attempt is a revision (counts toward D2).
REVISION_FROM = frozenset({StepState.ACCEPTING, StepState.BLOCKED_BY_GATE, StepState.TIMEOUT})

TERMINAL_STEP_STATES = frozenset(
    {StepState.ACCEPTED, StepState.FAILED_AFTER_CAP, StepState.CANCELLED}
)

#: Every legal step move. ``running`` out of a ``REVISION_FROM`` state needs
#: revision budget; ``failed_after_cap`` needs it exhausted (D2).
STEP_TRANSITIONS: Mapping[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset(
        {StepState.RUNNING, StepState.NEEDS_INPUT, StepState.CANCELLED}
    ),
    StepState.RUNNING: frozenset(
        {
            StepState.ACCEPTING,
            StepState.NEEDS_INPUT,
            StepState.BLOCKED_BY_GATE,
            StepState.TIMEOUT,
            StepState.INTERRUPTED,
            StepState.CANCELLED,
        }
    ),
    StepState.ACCEPTING: frozenset(
        {
            StepState.ACCEPTED,
            StepState.RUNNING,
            StepState.FAILED_AFTER_CAP,
            StepState.CANCELLED,
        }
    ),
    StepState.BLOCKED_BY_GATE: frozenset(
        {StepState.RUNNING, StepState.FAILED_AFTER_CAP, StepState.CANCELLED}
    ),
    StepState.TIMEOUT: frozenset(
        {StepState.RUNNING, StepState.FAILED_AFTER_CAP, StepState.CANCELLED}
    ),
    StepState.NEEDS_INPUT: frozenset({StepState.PENDING, StepState.CANCELLED}),
    StepState.INTERRUPTED: frozenset({StepState.PENDING, StepState.CANCELLED}),
    StepState.ACCEPTED: frozenset(),
    StepState.FAILED_AFTER_CAP: frozenset(),
    StepState.CANCELLED: frozenset(),
}


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkLedgerError(f"{name} must be a timezone-aware datetime")


def _require_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise WorkLedgerError("run_id must be 1-64 chars of [A-Za-z0-9_.-]")


def _digest(parts: list[Any]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def step_key(run_id: str, ordinal: int, slug: str) -> str:
    """Deterministic step key: same run, ordinal and slug give the same key."""
    _require_run_id(run_id)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= 999:
        raise WorkLedgerError("ordinal must be an int in 1..999")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise WorkLedgerError("slug must be 1-40 chars of [a-z0-9-], no edge dash")
    return f"s{ordinal:03d}-{slug}-{_digest(['step', run_id, ordinal, slug])[:12]}"


def idempotency_key(run_id: str, key: str, attempt: int, action: str) -> str:
    """Key for one side effect of one attempt of one step; replays reuse it."""
    _require_run_id(run_id)
    if not isinstance(key, str) or not key:
        raise WorkLedgerError("step key must be a non-empty string")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise WorkLedgerError("attempt must be an int >= 1")
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise WorkLedgerError("action must match [a-z][a-z0-9_.:-]{0,63}")
    return _digest(["idem", run_id, key, attempt, action])


@dataclass(frozen=True)
class WorkRun:
    run_id: str
    state: RunState
    created_at: datetime
    updated_at: datetime
    size: RunSize | None = None
    #: The active state a wait state returns to; None outside wait states.
    resume_state: RunState | None = None

    def __post_init__(self) -> None:
        _require_run_id(self.run_id)
        if not isinstance(self.state, RunState):
            raise WorkLedgerError("state must be a RunState")
        if self.size is not None and not isinstance(self.size, RunSize):
            raise WorkLedgerError("size must be a RunSize or None")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise WorkLedgerError("updated_at precedes created_at")
        if self.state in WAIT_RUN_STATES:
            if self.resume_state not in ACTIVE_RUN_STATES:
                raise WorkLedgerError("a waiting run needs an active resume_state")
        elif self.resume_state is not None:
            raise WorkLedgerError("resume_state is only set while waiting")


@dataclass(frozen=True)
class WorkStep:
    run_id: str
    key: str
    ordinal: int
    slug: str
    state: StepState
    updated_at: datetime
    depends_on: tuple[str, ...] = ()
    #: 1 for the first attempt; each revision adds one (D2 caps revisions).
    attempt: int = 1
    #: H2 attempt link for the current attempt, set by the dispatcher.
    attempt_link: str | None = None

    def __post_init__(self) -> None:
        if self.key != step_key(self.run_id, self.ordinal, self.slug):
            raise WorkLedgerError("key does not match run_id/ordinal/slug")
        if not isinstance(self.state, StepState):
            raise WorkLedgerError("state must be a StepState")
        _require_aware(self.updated_at, "updated_at")
        if not isinstance(self.depends_on, tuple) or not all(
            isinstance(dep, str) and dep for dep in self.depends_on
        ):
            raise WorkLedgerError("depends_on must be a tuple of step keys")
        if self.key in self.depends_on:
            raise WorkLedgerError("a step cannot depend on itself")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or not 1 <= self.attempt <= MAX_REVISIONS + 1
        ):
            raise WorkLedgerError(f"attempt must be an int in 1..{MAX_REVISIONS + 1}")
        if self.state is StepState.FAILED_AFTER_CAP and self.attempt != MAX_REVISIONS + 1:
            raise WorkLedgerError("failed_after_cap needs the revision cap reached (D2)")
        if self.attempt_link is not None and (
            not isinstance(self.attempt_link, str) or not self.attempt_link
        ):
            raise WorkLedgerError("attempt_link must be a non-empty string or None")

    @property
    def revisions(self) -> int:
        return self.attempt - 1


@dataclass(frozen=True)
class WorkEvent:
    run_id: str
    seq: int
    kind: EventKind
    at: datetime
    step_key: str | None = None
    #: Canonical JSON of the event payload (see ``event_payload_json``).
    payload_json: str = field(default="{}")

    def __post_init__(self) -> None:
        _require_run_id(self.run_id)
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise WorkLedgerError("seq must be an int >= 1")
        if not isinstance(self.kind, EventKind):
            raise WorkLedgerError("kind must be an EventKind")
        _require_aware(self.at, "at")
        if self.step_key is not None and (not isinstance(self.step_key, str) or not self.step_key):
            raise WorkLedgerError("step_key must be a non-empty string or None")
        try:
            decoded = json.loads(self.payload_json)
        except (TypeError, ValueError) as exc:
            raise WorkLedgerError("payload_json must be JSON") from exc
        if not isinstance(decoded, dict) or event_payload_json(decoded) != self.payload_json:
            raise WorkLedgerError("payload_json must be a canonical JSON object")


def event_payload_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON for an event payload (sorted keys, no NaN)."""
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkLedgerError("event payload must be JSON-serializable") from exc


def _require_forward(previous: datetime, at: datetime) -> None:
    _require_aware(at, "at")
    if at < previous:
        raise WorkLedgerError("transition time precedes the last update")


def advance_run(run: WorkRun, to: RunState, *, at: datetime) -> WorkRun:
    """Return ``run`` moved to ``to`` at ``at``; raise IllegalTransition."""
    if not isinstance(to, RunState) or to not in RUN_TRANSITIONS[run.state]:
        raise IllegalTransition(f"run {run.state.value} -> {to}")
    _require_forward(run.updated_at, at)
    if run.state is RunState.PLANNING and to is RunState.BUILDING:
        if run.size not in (RunSize.SMALL, RunSize.MEDIUM):
            raise IllegalTransition("planning -> building needs a small/medium run (D9)")
    if run.state is RunState.PLANNING and to is RunState.AWAITING_APPROVAL:
        if run.size is not RunSize.LARGE:
            raise IllegalTransition("planning -> awaiting_approval is for large runs")
    if run.state in WAIT_RUN_STATES:
        if to in ACTIVE_RUN_STATES and to is not run.resume_state:
            raise IllegalTransition(
                f"run resumes to {run.resume_state.value}, not {to.value}"
            )
        resume = run.resume_state if to in WAIT_RUN_STATES else None
    else:
        resume = run.state if to in WAIT_RUN_STATES else None
    return replace(run, state=to, updated_at=at, resume_state=resume)


def advance_step(
    step: WorkStep, to: StepState, *, at: datetime, attempt_link: str | None = None
) -> WorkStep:
    """Return ``step`` moved to ``to`` at ``at``; raise IllegalTransition.

    Entering ``running`` from a ``REVISION_FROM`` state starts a revision and
    is refused once ``MAX_REVISIONS`` are used; ``failed_after_cap`` is legal
    only then. A new attempt drops the previous ``attempt_link`` unless one is
    given.
    """
    if not isinstance(to, StepState) or to not in STEP_TRANSITIONS[step.state]:
        raise IllegalTransition(f"step {step.state.value} -> {to}")
    _require_forward(step.updated_at, at)
    attempt = step.attempt
    link = step.attempt_link if attempt_link is None else attempt_link
    if to is StepState.RUNNING and step.state in REVISION_FROM:
        if step.revisions >= MAX_REVISIONS:
            raise IllegalTransition(f"revision cap {MAX_REVISIONS} reached (D2)")
        attempt += 1
        link = attempt_link
    if to is StepState.FAILED_AFTER_CAP and step.revisions < MAX_REVISIONS:
        raise IllegalTransition("failed_after_cap needs the revision cap reached (D2)")
    return replace(step, state=to, updated_at=at, attempt=attempt, attempt_link=link)


def input_blocked_keys(steps: Iterable[WorkStep]) -> frozenset[str]:
    """D8: steps waiting on input plus every step that depends on one."""
    steps = list(steps)
    blocked = {s.key for s in steps if s.state is StepState.NEEDS_INPUT}
    changed = True
    while changed:
        changed = False
        for s in steps:
            if s.key not in blocked and blocked.intersection(s.depends_on):
                blocked.add(s.key)
                changed = True
    return frozenset(blocked)


def runnable_steps(steps: Iterable[WorkStep]) -> tuple[WorkStep, ...]:
    """Pending steps whose dependencies are all accepted, in ordinal order.

    A dependency on an unknown key never counts as accepted (fail closed).
    """
    steps = list(steps)
    accepted = {s.key for s in steps if s.state is StepState.ACCEPTED}
    blocked = input_blocked_keys(steps)
    ready = [
        s
        for s in steps
        if s.state is StepState.PENDING
        and s.key not in blocked
        and all(dep in accepted for dep in s.depends_on)
    ]
    return tuple(sorted(ready, key=lambda s: s.ordinal))


def run_waits_for_input(steps: Iterable[WorkStep]) -> bool:
    """D8: the run itself enters needs_input only when nothing else can move.

    True when some step waits on input and no step is in flight or runnable.
    """
    steps = list(steps)
    if not any(s.state is StepState.NEEDS_INPUT for s in steps):
        return False
    in_flight = {
        StepState.RUNNING,
        StepState.ACCEPTING,
        StepState.BLOCKED_BY_GATE,
        StepState.TIMEOUT,
        StepState.INTERRUPTED,
    }
    if any(s.state in in_flight for s in steps):
        return False
    return not runnable_steps(steps)


def run_should_pause(steps: Iterable[WorkStep]) -> bool:
    """D2: the run pauses once ``FAILED_STEPS_PAUSE`` steps failed after cap."""
    failed = sum(1 for s in steps if s.state is StepState.FAILED_AFTER_CAP)
    return failed >= FAILED_STEPS_PAUSE
