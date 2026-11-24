"""Deterministic focus decisions for the communication interface."""

from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum


class Intent(Enum):
    READ = "read"
    CHAT = "chat"
    WRITE = "write"
    ACTION = "action"
    START_PROJECT = "start_project"


class Action(Enum):
    SWITCH = "SWITCH"
    ASK = "ASK"
    OPEN_PROJECT = "OPEN_PROJECT"


@dataclass(frozen=True)
class Focus:
    project: str | None
    topic: str


@dataclass(frozen=True)
class Proposal:
    intent: Intent
    topic: str
    # None means topic-only. An empty tuple means project resolution found no target.
    candidates: tuple[str, ...] | None = None
    explicit: bool = False
    confidence: float | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    focus: Focus
    notice: str | None = None
    question: str | None = None


def decide(current: Focus, proposal: Proposal, projects: Collection[str]) -> Decision:
    """Return an action and the resulting focus without changing shared state."""
    if not isinstance(proposal.intent, Intent):
        raise ValueError(f"Unknown focus intent: {proposal.intent!r}")

    candidates = proposal.candidates
    opening = proposal.intent is Intent.START_PROJECT
    if candidates is None and not opening:
        return Decision(Action.SWITCH, Focus(current.project, proposal.topic))

    if not candidates or len(candidates) != 1 or candidates[0] not in projects:
        return Decision(Action.ASK, current, question="Which project?")

    target = Focus(candidates[0], proposal.topic)
    if opening:
        return Decision(Action.OPEN_PROJECT, target)
    if target.project == current.project and target.topic != current.topic:
        return Decision(Action.SWITCH, target)
    if proposal.explicit or proposal.intent in (Intent.READ, Intent.CHAT):
        return Decision(Action.SWITCH, target, notice="Switched")
    return Decision(Action.ASK, current, question=f"Switch to {target.project}?")


@dataclass(frozen=True)
class Job:
    scope: Focus

    def report_scope(self) -> Focus:
        return self.scope


class FocusState:
    """The focus holder shared by both views."""

    def __init__(self, focus: Focus) -> None:
        self._focus = focus

    @property
    def focus(self) -> Focus:
        return self._focus

    def apply(self, proposal: Proposal, projects: Collection[str]) -> Decision:
        decision = decide(self.focus, proposal, projects)
        if decision.action is not Action.ASK:
            self._focus = decision.focus
        return decision

    def start_job(self) -> Job:
        return Job(self.focus)
