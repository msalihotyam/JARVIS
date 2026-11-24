"""PM36 lesson classification and guard proposals for PM31.

Callers supply typed signals and stable invariant identifiers, the same across
sources for the same defect. Raw logs, review prose, and corrections never
become instructions or write targets. The ledger has no lesson event kind,
so the caller retains the returned classes and supplies them on later calls.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import re

from jarvis.work_plan import PlanSlice


class LessonError(ValueError):
    """A lesson input does not match the closed schema."""


class SliceOutcome(str, Enum):
    FAILED = "failed"
    BLOCKED = "blocked"


class FailureSignal(str, Enum):
    TEST_FAILURE = "test_failure"
    MISSING_TEST = "missing_test"
    STATIC_DEFECT = "static_defect"
    VALIDATION_GAP = "validation_gap"
    PLAN_GAP = "plan_gap"
    POLICY = "policy"
    AUTHORITY = "authority"


class FailureKind(str, Enum):
    REGRESSION = "regression"
    STATIC = "static"
    VALIDATION = "validation"
    PLANNING = "planning"
    POLICY = "policy"
    AUTHORITY = "authority"


class GuardKind(str, Enum):
    TEST = "test"
    LINT_RULE = "lint rule"
    VALIDATOR_RULE = "validator rule"
    PLANNER_INSTRUCTION = "planner instruction"


@dataclass(frozen=True)
class FailedSlice:
    outcome: SliceOutcome
    signal: FailureSignal
    invariant: str


@dataclass(frozen=True)
class ReviewFinding:
    signal: FailureSignal
    invariant: str


@dataclass(frozen=True)
class SalihCorrection:
    signal: FailureSignal
    invariant: str


_INVARIANT = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_KINDS = {
    FailureSignal.TEST_FAILURE: FailureKind.REGRESSION,
    FailureSignal.MISSING_TEST: FailureKind.REGRESSION,
    FailureSignal.STATIC_DEFECT: FailureKind.STATIC,
    FailureSignal.VALIDATION_GAP: FailureKind.VALIDATION,
    FailureSignal.PLAN_GAP: FailureKind.PLANNING,
    FailureSignal.POLICY: FailureKind.POLICY,
    FailureSignal.AUTHORITY: FailureKind.AUTHORITY,
}
_GUARDS = {
    FailureKind.REGRESSION: (
        GuardKind.TEST, "tests/test_pm36_guards.py",
        "Add a regression test that reproduces the failed invariant.",
    ),
    FailureKind.STATIC: (
        GuardKind.LINT_RULE, "jarvis/project_guard_lint.py",
        "Add a lint rule that detects the recurring static defect.",
    ),
    FailureKind.VALIDATION: (
        GuardKind.VALIDATOR_RULE, "jarvis/project_guard_validation.py",
        "Add a validator rule that rejects the invalid input shape.",
    ),
    FailureKind.PLANNING: (
        GuardKind.PLANNER_INSTRUCTION, "docs/project_planner_guards.md",
        "Record the missing planning constraint for future plans.",
    ),
    FailureKind.POLICY: (
        GuardKind.VALIDATOR_RULE, "jarvis/work_plan.py",
        "Propose a validator rule that enforces the affected policy.",
    ),
    FailureKind.AUTHORITY: (
        GuardKind.VALIDATOR_RULE, "jarvis/core.py",
        "Propose a validator rule that enforces the affected approval boundary.",
    ),
}


@dataclass(frozen=True)
class FailureClass:
    kind: FailureKind
    invariant: str

    def __post_init__(self) -> None:
        if type(self.kind) is not FailureKind:
            raise LessonError("invalid failure kind")
        if (type(self.invariant) is not str or len(self.invariant) > 128
                or not _INVARIANT.fullmatch(self.invariant)):
            raise LessonError("expected a stable invariant identifier")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.invariant}"


@dataclass(frozen=True)
class GuardProposal:
    failure: FailureClass
    kind: GuardKind
    target: str
    why: str
    slice: PlanSlice
    needs_salih: bool


@dataclass(frozen=True)
class Lessons:
    classes: tuple[FailureClass, ...]
    proposals: tuple[GuardProposal, ...]


def classify(record: FailedSlice | ReviewFinding | SalihCorrection) -> FailureClass:
    """Classify one observation without treating its source as approval."""
    if type(record) not in (FailedSlice, ReviewFinding, SalihCorrection):
        raise LessonError("unknown observation record")
    if type(record) is FailedSlice and type(record.outcome) is not SliceOutcome:
        raise LessonError("expected a failed or blocked slice")
    if type(record.signal) is not FailureSignal:
        raise LessonError("unknown failure signal")
    return FailureClass(_KINDS[record.signal], record.invariant)


def _known_classes(known: tuple[FailureClass, ...]) -> set[FailureClass]:
    if type(known) is not tuple or any(type(item) is not FailureClass for item in known):
        raise LessonError("expected a tuple of known failure classes")
    return set(known)


def _guard(failure: FailureClass) -> GuardProposal:
    kind, target, why = _GUARDS[failure.kind]
    digest = hashlib.sha256(failure.key.encode("ascii")).hexdigest()
    slice_proposal = PlanSlice(f"pm36-{digest[:32]}", why, (target,), ())
    needs_salih = failure.kind in (FailureKind.POLICY, FailureKind.AUTHORITY)
    return GuardProposal(failure, kind, target, why, slice_proposal, needs_salih)


def learn(
    records: tuple[FailedSlice | ReviewFinding | SalihCorrection, ...],
    *,
    known: tuple[FailureClass, ...] = (),
) -> Lessons:
    """Return one class per observation and one proposal per previously unseen key."""
    covered = _known_classes(known)
    if type(records) is not tuple:
        raise LessonError("expected a tuple of observations")
    classes = tuple(classify(record) for record in records)
    new = set(classes) - covered
    return Lessons(classes, tuple(_guard(item) for item in sorted(new, key=lambda item: item.key)))


def planner_lessons(known: tuple[FailureClass, ...]) -> tuple[GuardProposal, ...]:
    """Expose known classes and their proposed guards, with no approval implied."""
    classes = sorted(_known_classes(known), key=lambda item: item.key)
    return tuple(_guard(item) for item in classes)
