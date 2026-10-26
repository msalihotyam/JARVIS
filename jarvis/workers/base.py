import abc
from dataclasses import dataclass

@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str
    changed_files: tuple[str, ...]
    evidence: tuple[str, ...]

    def __post_init__(self):
        valid_statuses = {"completed", "partial", "failed", "timeout", "blocked_by_gate"}
        if self.status not in valid_statuses:
            raise ValueError(f"invalid status: {self.status}")

@dataclass(frozen=True)
class AgentProfile:
    name: str
    tools: frozenset[str]
    paths: frozenset[str]
    network: bool
    max_turns: int
    budget_sessions: int

    def __post_init__(self):
        if self.max_turns <= 0:
            raise ValueError("max_turns must be > 0")
        if self.budget_sessions < 0:
            raise ValueError("budget_sessions must be >= 0")

class Worker(abc.ABC):
    @abc.abstractmethod
    def start(self, task: str) -> str:
        pass

    @abc.abstractmethod
    def status(self, run_id: str) -> str:
        pass

    @abc.abstractmethod
    def stop(self, run_id: str) -> None:
        pass

    @abc.abstractmethod
    def collect(self, run_id: str) -> WorkerResult:
        pass

def effective_authority(*profiles: AgentProfile) -> AgentProfile:
    if not profiles:
        raise ValueError("at least one profile is required")

    first = profiles[0]
    tools = first.tools.intersection(*(p.tools for p in profiles[1:]))
    paths = first.paths.intersection(*(p.paths for p in profiles[1:]))
    network = all(p.network for p in profiles)
    max_turns = min(p.max_turns for p in profiles)
    budget_sessions = min(p.budget_sessions for p in profiles)

    return AgentProfile(
        name="effective",
        tools=tools,
        paths=paths,
        network=network,
        max_turns=max_turns,
        budget_sessions=budget_sessions
    )
