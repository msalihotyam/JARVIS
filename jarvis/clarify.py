"""Project-mode clarification contracts (PM06): ProjectBrief and the exit check.

Pure and deterministic; nothing here is wired into a run yet. The exit check
is fail-closed: every required slot filled, no open question, every acceptance
example convertible to a check (or marked manual, which forces the large
class), and a read-back of the *current* brief confirmed by the user in a
turn after it was shown. A model claiming it understood is not an input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

SLOTS = (
    "goal",
    "users",
    "platform",
    "target_location",
    "definition_of_done",
    "constraints",
    "out_of_scope",
    "data_sensitivity",
    "outgoing_or_money",
)

# Rounds are counted in total, initial round included: asking round 4 is
# refused and the run goes to needs_input.
MAX_ROUNDS = 3

NEEDS_INPUT = "needs_input"
CLARIFYING = "clarifying"
PLANNING = "planning"


@dataclass(frozen=True)
class AcceptanceExample:
    text: str
    check: str = ""  # how it becomes a deterministic check
    manual: bool = False  # "manual check by Salih"; forces the large class


@dataclass(frozen=True)
class ProjectBrief:
    goal: str = ""
    users: str = ""
    platform: str = ""
    target_location: str = ""
    definition_of_done: str = ""
    acceptance_examples: tuple[AcceptanceExample, ...] = ()
    constraints: str = ""
    out_of_scope: str = ""
    data_sensitivity: str = ""
    outgoing_or_money: bool | None = None  # None = not answered


def _example_ok(example: AcceptanceExample) -> bool:
    return bool(example.text.strip()) and (bool(example.check.strip()) or example.manual)


def empty_slots(brief: ProjectBrief) -> tuple[str, ...]:
    empty = []
    for slot in SLOTS:
        if slot == "outgoing_or_money":
            filled = isinstance(brief.outgoing_or_money, bool)
        elif slot == "definition_of_done":
            examples = brief.acceptance_examples
            filled = (
                bool(brief.definition_of_done.strip())
                and bool(examples)
                and all(_example_ok(e) for e in examples)
            )
        else:
            value = getattr(brief, slot)
            filled = isinstance(value, str) and bool(value.strip())
        if not filled:
            empty.append(slot)
    return tuple(empty)


def brief_hash(brief: ProjectBrief) -> str:
    payload = json.dumps(asdict(brief), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReadBack:
    brief_hash: str
    shown_turn: int


@dataclass(frozen=True)
class ClarifyState:
    brief: ProjectBrief
    rounds_asked: int = 0
    open_questions: tuple[str, ...] = ()
    readback: ReadBack | None = None
    confirmed_turn: int | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    large_forced: bool = False


def check_understood(state: ClarifyState) -> Verdict:
    reasons = [f"empty slot: {slot}" for slot in empty_slots(state.brief)]
    reasons += [f"open question: {q}" for q in state.open_questions]
    readback = state.readback
    if readback is None:
        reasons.append("read-back not shown")
    elif readback.brief_hash != brief_hash(state.brief):
        reasons.append("brief changed after read-back")
    elif state.confirmed_turn is None:
        reasons.append("read-back not confirmed")
    elif state.confirmed_turn <= readback.shown_turn:
        reasons.append("confirmation not in a later turn than the read-back")
    large = any(e.manual for e in state.brief.acceptance_examples)
    return Verdict(ok=not reasons, reasons=tuple(reasons), large_forced=large)


def next_step(state: ClarifyState) -> str:
    """planning once understood; needs_input when another round would pass the cap."""
    if check_understood(state).ok:
        return PLANNING
    if empty_slots(state.brief) or state.open_questions:
        if state.rounds_asked >= MAX_ROUNDS:
            return NEEDS_INPUT
    return CLARIFYING


@dataclass(frozen=True)
class Question:
    slot: str
    text: str


@dataclass(frozen=True)
class QuestionSet:
    round: int
    questions: tuple[Question, ...]

    def render(self) -> str:
        return "\n".join(f"{i}. {q.text.strip()}" for i, q in enumerate(self.questions, 1))


def validate_question_set(qs: QuestionSet, brief: ProjectBrief) -> None:
    """All questions go in one numbered message; follow-ups target empty slots only."""
    if not 1 <= qs.round <= MAX_ROUNDS:
        raise ValueError(f"round {qs.round} outside 1..{MAX_ROUNDS}: {NEEDS_INPUT}")
    if not qs.questions:
        raise ValueError("question set is empty")
    empty = set(empty_slots(brief))
    for q in qs.questions:
        if q.slot not in SLOTS:
            raise ValueError(f"unknown slot: {q.slot!r}")
        if not q.text.strip():
            raise ValueError(f"blank question for slot {q.slot}")
        if qs.round > 1 and q.slot not in empty:
            raise ValueError(f"follow-up round asks filled slot {q.slot}")


@dataclass(frozen=True)
class MidBuildQuestion:
    """D8: ask mid-build only for a decision that changes the result."""

    decision: str
    options: tuple[str, ...]
    default: str  # JARVIS's default if nobody answers
    blocks: tuple[str, ...]  # slice ids that cannot proceed without the answer
    changes_result: bool

    def __post_init__(self) -> None:
        if not self.changes_result:
            raise ValueError("mid-build question must name a decision that changes the result")
        if not self.decision.strip():
            raise ValueError("mid-build question has no decision")
        opts = [o.strip() for o in self.options]
        if len(opts) < 2 or len(set(opts)) != len(opts) or not all(opts):
            raise ValueError("mid-build question needs at least two distinct options")
        if self.default.strip() not in opts:
            raise ValueError("JARVIS default must be one of the options")
        if not self.blocks or not all(b.strip() for b in self.blocks):
            raise ValueError("mid-build question must name the steps it blocks")


@dataclass(frozen=True)
class MidBuildBatch:
    questions: tuple[MidBuildQuestion, ...]
    message: str
    blocked: frozenset[str]
    runnable: tuple[str, ...] = field(default=())


def batch_mid_build(questions, slices) -> MidBuildBatch:
    """Merge by decision, number them in one message; only dependent slices block."""
    slices = tuple(slices)
    merged: dict[str, MidBuildQuestion] = {}
    for q in questions:
        key = q.decision.strip()
        prior = merged.get(key)
        if prior is None:
            merged[key] = q
            continue
        if (prior.options, prior.default) != (q.options, q.default):
            raise ValueError(f"conflicting questions for decision {key!r}")
        blocks = prior.blocks + tuple(b for b in q.blocks if b not in prior.blocks)
        merged[key] = MidBuildQuestion(key, q.options, q.default, blocks, True)
    if not merged:
        raise ValueError("no mid-build questions to batch")
    blocked = frozenset(b for q in merged.values() for b in q.blocks)
    unknown = blocked - set(slices)
    if unknown:
        raise ValueError(f"blocks unknown slices: {sorted(unknown)}")
    lines = []
    for i, q in enumerate(merged.values(), 1):
        lines.append(
            f"{i}. {q.decision} Options: {' / '.join(q.options)}. "
            f"Default: {q.default}. Blocks: {', '.join(q.blocks)}."
        )
    return MidBuildBatch(
        questions=tuple(merged.values()),
        message="\n".join(lines),
        blocked=blocked,
        runnable=tuple(s for s in slices if s not in blocked),
    )
